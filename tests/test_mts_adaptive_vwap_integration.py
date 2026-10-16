"""2026-10-16 Hermes Agent: runtime shadow seam and offline replay checks."""
from collections import deque
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import csv
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from strategies.plugins.futures.active.mts_adaptive_vwap import AdaptiveVWAP, AdaptiveVWAPConfig
from strategies.plugins.futures.active import mts_adaptive_ab as ab
from strategies.plugins.futures.active.mts_hvwap_candidate import evaluate_hvwap_candidate
from strategies.plugins.futures.active.mts_oi_estimator import OISnapshot


TW = timezone(timedelta(hours=8))
TS = datetime(2026, 10, 15, 10, tzinfo=TW).timestamp()


def make_strategy(monkeypatch, tmp_path):
    from strategies.plugins.futures.active import tmf_spread as mts
    path = tmp_path / "paper" / "events.jsonl"
    path.parent.mkdir()
    path.write_text("\n".join(json.dumps(e) for e in [
        {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:58:00+08:00"},
        {"event": "LEG_FILLED", "order_id": "A", "ts": "2026-10-15T09:59:00+08:00"}]) + "\n")
    monkeypatch.setattr(mts, "_MTS_EVENT_LOG", str(path))
    events = []
    monkeypatch.setattr(mts, "_append_event", lambda name, **payload: events.append((name, payload)))
    obj = mts.TMFSpread.__new__(mts.TMFSpread)
    obj._hvwap_5m_bars = deque([{"ts": TS - 300, "close": 100, "volume": 467,
                                "far_close": 90, "far_volume": 210},
                               {"ts": TS, "close": 110, "volume": 467,
                                "far_close": 91, "far_volume": 210}])
    obj._has_position = True
    obj._near_side, obj._far_side = "LONG", "SHORT"
    obj._build_trend_release_input = lambda: (False, None)
    obj._adaptive_vwap_config = AdaptiveVWAPConfig()
    return obj, events


def tick(obj):
    obj._hvwap_candidate_tick({"ts": TS, "close": 110, "far_close": 91, "volume": 9999,
                               "atr": 10}, datetime.fromtimestamp(TS, TW))


def test_shadow_pair_cadence_mode_path_and_quality_source(monkeypatch, tmp_path):
    obj, events = make_strategy(monkeypatch, tmp_path)
    tick(obj)
    assert [name for name, _ in events] == ["HVWAP_CANDIDATE", "HVWAP_ADAPTIVE_AB"]
    assert obj._hvwap_last_verdict.adaptive_price is None
    pair = obj._adaptive_last_pair
    adaptive = pair["adaptive"]
    assert adaptive["vol_quality_score"] == 70  # supplied stacking rule sees ENTRY + FILL
    assert adaptive["vol_quality"] == 100  # distinct Z-score quality
    assert adaptive["chips_strength"] == AdaptiveVWAP._chips_strength(.2, 1, 70, 0)
    assert adaptive["adaptive_twap"] == 110
    assert pair["baseline"]["status"] == adaptive["status"]
    assert pair["release_action_emitted"] is False
    paths = list((tmp_path / "paper" / "ab_test").glob("*.jsonl"))
    assert len(paths) == 1
    assert json.loads(paths[0].read_text())["pair_id"] == pair["pair_id"]
    tick(obj)
    assert len(events) == 2
    assert len(paths[0].read_text().splitlines()) == 1


def test_disabled_and_failed_shadow_preserve_legacy(monkeypatch, tmp_path, caplog):
    obj, events = make_strategy(monkeypatch, tmp_path)
    obj._adaptive_vwap_config = replace(obj._adaptive_vwap_config, shadow_enabled=False)
    tick(obj)
    assert [name for name, _ in events] == ["HVWAP_CANDIDATE"]
    obj._hvwap_last_emit_bucket = None
    obj._adaptive_vwap_config = AdaptiveVWAPConfig()
    def fail(**kwargs):
        raise ValueError("shadow failed")
    monkeypatch.setattr(ab, "evaluate_hvwap_ab", fail)
    tick(obj)
    assert [name for name, _ in events] == ["HVWAP_CANDIDATE", "HVWAP_CANDIDATE"]
    assert "shadow evaluation unavailable" in caplog.text
    assert obj._hvwap_last_verdict is not None


def test_config_is_loaded_at_init_and_invalid_file_disables_shadow(tmp_path, configured_ticker):
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    context = SimpleNamespace(market=SimpleNamespace(ticker=configured_ticker), config={})
    obj = TMFSpread.__new__(TMFSpread)
    obj.init(context)
    assert isinstance(obj._adaptive_vwap_config, AdaptiveVWAPConfig)
    context.config["adaptive_vwap_config_path"] = str(tmp_path / "absent.yaml")
    obj.init(context)
    assert obj._adaptive_vwap_config is None


def test_off_segment_and_mismatched_snapshot_stay_unavailable():
    def evaluate(ts, oi_ts):
        return evaluate_hvwap_candidate(decision_ts=ts,
            near_bars=[{"ts": ts, "close": 100, "volume": 467}],
            far_bars=[{"ts": ts, "close": 90, "volume": 210}],
            near_price=100, far_price=90, near_side="LONG", far_side="SHORT", atr_15m=10,
            oi_snapshot=OISnapshot(oi_ts, 1, .2, 1, 1, 0, 1, 99), event_sequence=[], use_adaptive=True)
    assert evaluate(TS, TS - 300).adaptive_price is None
    noon = datetime(2026, 10, 15, 12, tzinfo=TW).timestamp()
    assert evaluate(noon, noon).adaptive_price is None


def replay_module():
    path = Path(__file__).parents[1] / "scripts/research/replay_adaptive_vwap.py"
    spec = importlib.util.spec_from_file_location("adaptive_replay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write_minute_csv(path):
    with path.open("w") as stream:
        writer = csv.writer(stream)
        writer.writerow(["ts", "Open", "High", "Low", "Close", "Volume"])
        for day in range(5):
            start = datetime(2026, 6, 8 + day, 9, 0, tzinfo=TW)
            for minute in range(30):
                dt = start + timedelta(minutes=minute)
                writer.writerow([dt.isoformat(), 100 + minute, 102 + minute, 99 + minute, 101 + minute, 10])


def test_offline_five_date_replay_collects_pairs_without_inventing_outcomes(tmp_path):
    module = replay_module()
    near, far = tmp_path / "near.csv", tmp_path / "far.csv"
    write_minute_csv(near)
    write_minute_csv(far)
    result = module.replay([near], [far], start_date="2026-06-08", end_date="2026-06-12",
                           output_dir=tmp_path / "run", timestamp_kind="start")
    assert result["paired_decisions"] == 30
    assert len(result["observed_dates"]) == 5
    assert result["valid_adaptive_decisions"] == 0
    assert result["comparison"]["promotion"] == "NO_GO"
    assert "OI_QUALITY_EVENTS_MISSING" in result["data_gaps"]
    assert len(list((tmp_path / "run/logs/ab_test").glob("*.jsonl"))) == 5
    assert len(list((tmp_path / "run/reports").glob("*.md"))) == 1
    with pytest.raises(ValueError):
        module.replay([near], [far], start_date="2026-06-08", end_date="2026-06-12",
                      output_dir=tmp_path / "run", timestamp_kind="start")


def test_replay_resampling_rejects_gaps_and_bad_timestamp_contract(tmp_path):
    module = replay_module()
    path = tmp_path / "bars.csv"
    write_minute_csv(path)
    assert len(module.load_completed_bars([path, path], timestamp_kind="start", source_bar_secs=60)) == 30
    for kind, seconds in (("unknown", 60), ("start", 61), ("start", 0)):
        with pytest.raises(ValueError):
            module.load_completed_bars([path], timestamp_kind=kind, source_bar_secs=seconds)
    rows = path.read_text().splitlines()
    path.write_text("\n".join([rows[0]] + rows[2:]) + "\n")
    assert len(module.load_completed_bars([path], timestamp_kind="start", source_bar_secs=60)) == 29


def test_replay_event_signals_and_paired_outcome_join(tmp_path):
    module = replay_module()
    near, far = tmp_path / "near.csv", tmp_path / "far.csv"
    write_minute_csv(near)
    write_minute_csv(far)
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(json.dumps({"event": kind, "order_id": f"O{day}",
        "ts": datetime(2026, 6, 8 + day, 9, 26, tzinfo=TW).isoformat()})
        for day in range(5) for kind in ("ENTRY", "LEG_FILLED")) + "\n")
    outcome_file = tmp_path / "outcomes.jsonl"
    outcome_file.write_text("\n".join(json.dumps({
        # 2026-10-16 Hermes Agent: collector IDs use integer bucket-close epochs.
        "pair_id": f"replay:{int(datetime(2026, 6, 8 + day, 9, 30, tzinfo=TW).timestamp())}",
        "ts": datetime(2026, 6, 8 + day, 9, 30, tzinfo=TW).timestamp(),
        "baseline": {"net_pnl": -10 if day == 0 else 10},
        "adaptive": {"net_pnl": -5 if day == 0 else 20}}) for day in range(5)) + "\n")
    report = module.replay([near], [far], start_date="2026-06-08", end_date="2026-06-12",
        output_dir=tmp_path / "with-signals", timestamp_kind="start", events_path=events,
        outcomes_path=outcome_file)
    assert report["valid_adaptive_decisions"] == 20  # 09:15 boundary inclusive: four bars per date
    assert report["comparison"]["paired_count"] == 5
    assert report["comparison"]["promotion"] == "NO_GO"  # equal win rates fail the required gain
    # 2026-10-16 Hermes Agent: valid evaluated pairs may pass the numerical gate;
    # off-segment observations remain reported, while invalid evaluated pairs veto it.
    favorable = [json.loads(line) for line in outcome_file.read_text().splitlines()]
    favorable[0]["adaptive"]["net_pnl"] = 5
    outcome_file.write_text("\n".join(json.dumps(row) for row in favorable) + "\n")
    go_report = module.replay([near], [far], start_date="2026-06-08", end_date="2026-06-12",
        output_dir=tmp_path / "valid-paired-outcomes", timestamp_kind="start", events_path=events,
        outcomes_path=outcome_file)
    assert go_report["comparison"]["promotion"] == "GO_CANDIDATE"
    for day, row in enumerate(favorable):
        row["ts"] = datetime(2026, 6, 8 + day, 9, 5, tzinfo=TW).timestamp()
        row["pair_id"] = f"replay:{int(row['ts'])}"
    outcome_file.write_text("\n".join(json.dumps(row) for row in favorable) + "\n")
    blocked = module.replay([near], [far], start_date="2026-06-08", end_date="2026-06-12",
        output_dir=tmp_path / "unavailable-paired-outcomes", timestamp_kind="start", events_path=events,
        outcomes_path=outcome_file)
    assert blocked["comparison"]["promotion"] == "NO_GO"
    assert "OUTCOMES_WITH_UNAVAILABLE_ADAPTIVE_INPUTS" in blocked["data_gaps"]
    outcome_file.write_text(json.dumps({"pair_id": "foreign", "ts": 0}) + "\n")
    with pytest.raises(ValueError, match="outside this replay"):
        module.replay([near], [far], start_date="2026-06-08", end_date="2026-06-12",
            output_dir=tmp_path / "invalid-outcomes", timestamp_kind="start", events_path=events,
            outcomes_path=outcome_file)


def test_empty_replay_and_cli(tmp_path, capsys):
    module = replay_module()
    result = module.replay([], [], start_date="2026-10-07", end_date="2026-10-11",
        output_dir=tmp_path / "empty", timestamp_kind="start")
    assert "NO_MATCHING_COMPLETED_BARS" in result["data_gaps"]
    with pytest.raises(ValueError):
        module.replay([], [], start_date="2026-10-12", end_date="2026-10-11",
            output_dir=tmp_path / "bad-date", timestamp_kind="start")
    module.main(["--near-glob", str(tmp_path / "absent-near*.csv"),
                 "--far-glob", str(tmp_path / "absent-far*.csv"), "--start", "2026-10-07",
                 "--end", "2026-10-11", "--output-dir", str(tmp_path / "cli"),
                 "--timestamp-kind", "start", "--initial-equity", "40000"])
    assert json.loads(capsys.readouterr().out)["paired_decisions"] == 0
