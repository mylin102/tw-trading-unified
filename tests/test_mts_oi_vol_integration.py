# 2026-10-15 Hermes Agent: verify dispatch integration without changing supplied algorithms.
from collections import deque
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path

import pytest

from strategies.plugins.futures.active.mts_hvwap_candidate import (
    HvwapCandidateVerdict, evaluate_hvwap_candidate,
)
from strategies.plugins.futures.active.mts_oi_estimator import OISnapshot, OIEstimator
from strategies.plugins.futures.active.mts_vol_adjuster import VolAdjuster


TW = timezone(timedelta(hours=8))
# 2026-10-20 Hermes Agent: derive volume expectations from the committed calibration artifact.
VOLUME_PARAMS = json.loads((Path(__file__).parents[1] / "config/mts_vol_zscore_params.json").read_text())["params"]


def calibrated_z(volume, segment):
    params = VOLUME_PARAMS[segment]
    return (volume - params["mean"]) / params["std"] if params["std"] else 0.0


def calibrated_quality(volume, segment):
    z = calibrated_z(volume, segment)
    return 100 * math.exp(-(z ** 2) / 2)
SIGNALS = {"oi_intensity", "oi_direction", "oi_quality", "vol_zscore",
           "vol_anomaly_level", "vol_quality"}


def epoch(hour=10, minute=0):
    return datetime(2026, 10, 15, hour, minute, tzinfo=TW).timestamp()


def evaluate(bars, **kwargs):
    return evaluate_hvwap_candidate(
        decision_ts=epoch(), near_bars=bars,
        far_bars=[dict(b, close=90) for b in bars],
        near_price=100, far_price=90, near_side="LONG", far_side="SHORT",
        atr_15m=10, **kwargs)


def test_signals_serialize_without_changing_arbitration():
    bars = [{"ts": epoch(), "close": 100, "volume": 467}]
    baseline = evaluate(bars)
    oi = OISnapshot(epoch(), 1.0, 0.4, 1.0, 2, 0, 2, 99.3)
    enriched = evaluate(bars, oi_snapshot=oi)
    data = enriched.to_dict()
    assert data["oi_intensity"] == 0.4
    assert data["oi_direction"] == 1.0
    assert data["oi_quality"] == 99.3
    assert data["vol_zscore"] == pytest.approx(calibrated_z(467, "day"))
    assert data["vol_anomaly_level"] == "NORMAL"
    assert data["vol_quality"] == pytest.approx(calibrated_quality(467, "day"))
    assert replace(enriched, oi_intensity=None, oi_direction=None, oi_quality=None) == baseline


# 2026-10-15 Hermes Agent: explicit optional-schema regression requested by user.
def test_all_six_signal_fields_default_to_none():
    signal_fields = {f.name: f for f in fields(HvwapCandidateVerdict) if f.name in SIGNALS}
    assert signal_fields.keys() == SIGNALS
    assert all(f.default is None for f in signal_fields.values())
    assert all("Optional[" in f.type for f in signal_fields.values())


def test_latest_bar_by_timestamp_uses_near_volume_only():
    bars = [{"ts": epoch(), "close": 100, "volume": 709},
            {"ts": epoch(9, 55), "close": 100, "volume": 467}]
    result = evaluate(bars)
    assert result.vol_zscore == pytest.approx(calibrated_z(709, "day"))
    assert result.vol_anomaly_level == "NORMAL"  # (P) JSON-derived z is within the normal band.


@pytest.mark.parametrize("volume", [None, -1, float("nan"), float("inf")])
def test_invalid_volume_stays_unavailable(volume):
    result = evaluate([{"ts": epoch(), "close": 100, "volume": volume}])
    assert result.vol_zscore is None
    assert result.vol_anomaly_level is None
    assert result.vol_quality is None


def test_incomplete_bar_remains_fail_closed():
    result = evaluate([{"ts": epoch(10, 5), "close": 100, "volume": 467}])
    assert result.block_reason == "INCOMPLETE_BARS"
    assert all(result.to_dict()[key] is None for key in SIGNALS)


# 2026-10-20 Hermes Agent: S1 replaces the obsolete sub-session boundaries with TAIFEX sessions.
@pytest.mark.parametrize("hour,minute,segment", [
    (8, 44, None), (8, 45, "day"), (13, 45, "day"),
    (13, 46, None), (14, 59, None), (15, 0, "night"),
    (4, 59, "night"), (5, 0, "night"), (5, 1, None),
])
def test_dispatch_session_boundaries(hour, minute, segment):
    assert VolAdjuster.get_time_segment(epoch(hour, minute)) == segment


@pytest.mark.parametrize("segment", ["day", "night"])
def test_segment_calibration_and_exact_one_boundary(segment):
    params = VOLUME_PARAMS[segment]
    volume = params["mean"] + params["std"]
    z = VolAdjuster.compute_zscore(volume, segment)
    assert z == pytest.approx(1.0)
    assert VolAdjuster.classify_anomaly(1.0) == "NORMAL"  # S3 exact boundary; generated z is checked above.


def strategy(monkeypatch, tmp_path):
    from strategies.plugins.futures.active import tmf_spread as mts
    path = tmp_path / "scoped-events.jsonl"
    path.write_text(json.dumps({"event": "ENTRY", "order_id": "A",
                                "ts": "2026-10-15T09:58:00+08:00"}) + "\n")
    monkeypatch.setattr(mts, "_MTS_EVENT_LOG", str(path))
    monkeypatch.setenv("MTS_EVENT_LOG_PATH", str(tmp_path / "wrong-mode.jsonl"))
    events = []
    monkeypatch.setattr(mts, "_append_event", lambda name, **payload: events.append((name, payload)))
    obj = mts.TMFSpread.__new__(mts.TMFSpread)
    obj._hvwap_5m_bars = deque([{"ts": epoch(), "close": 100,
                                "volume": 467, "far_close": 90, "far_volume": 210}])
    obj._has_position = True
    obj._near_side, obj._far_side = "LONG", "SHORT"
    obj._build_trend_release_input = lambda: (False, None)
    return obj, events, path


def tick(obj):
    obj._hvwap_candidate_tick(
        {"ts": epoch(), "close": 100, "far_close": 90, "volume": 9999,
         "far_volume": 9999, "atr": 10}, datetime(2026, 10, 15, 10, tzinfo=TW))


def test_strategy_reads_writer_path_and_emits_completed_bar_signals(monkeypatch, tmp_path):
    obj, events, path = strategy(monkeypatch, tmp_path)
    tick(obj)
    assert obj._oi_estimator.log_path == str(path)
    assert obj._oi_snapshot.ts == epoch()
    assert obj._hvwap_last_verdict.oi_intensity == 0.2
    assert obj._hvwap_last_verdict.vol_zscore == pytest.approx(calibrated_z(467, "day"))
    assert len(events) == 1
    name, payload = events[0]
    assert name == "HVWAP_CANDIDATE"
    assert payload["oi_direction"] == 1.0
    assert payload["release_action_emitted"] is False
    assert SIGNALS <= payload.keys()
    monkeypatch.setattr(OIEstimator, "estimate_oi", lambda *args: pytest.fail("re-read same bar"))
    tick(obj)
    assert len(events) == 1


def test_optional_oi_failure_does_not_remove_hvwap_verdict(monkeypatch, tmp_path):
    obj, events, _ = strategy(monkeypatch, tmp_path)
    def fail(*args):
        raise OSError("unavailable event log")
    monkeypatch.setattr(OIEstimator, "estimate_oi", fail)
    tick(obj)
    assert obj._hvwap_last_verdict.oi_direction is None
    assert obj._hvwap_last_verdict.vol_zscore == pytest.approx(calibrated_z(467, "day"))
    assert events[0][0] == "HVWAP_CANDIDATE"


# 2026-10-15 Hermes Agent: path reconfiguration must never reuse another mode's cache.
def test_writer_path_change_invalidates_oi_cache(monkeypatch, tmp_path):
    from strategies.plugins.futures.active import tmf_spread as mts
    obj, _, _ = strategy(monkeypatch, tmp_path)
    tick(obj)
    other = tmp_path / "other-mode.jsonl"
    other.write_text(json.dumps({"event": "EXIT_LOG", "order_id": "B",
                                 "ts": "2026-10-15T09:58:00+08:00"}) + "\n")
    monkeypatch.setattr(mts, "_MTS_EVENT_LOG", str(other))
    tick(obj)
    assert obj._oi_estimator.log_path == str(other)
    assert obj._hvwap_last_verdict.oi_direction == 0.0


# 2026-10-15 Hermes Agent: expose the supplied estimator's current event-schema limitation.
def test_current_entry_schema_without_order_id_is_not_counted(tmp_path):
    path = tmp_path / "current-schema.jsonl"
    path.write_text(json.dumps({"event": "ENTRY", "trade_id": "trade-A",
                                "ts": "2026-10-15T09:58:00+08:00"}) + "\n")
    result = OIEstimator(str(path)).estimate_oi(epoch())
    assert result.new_orders == 0
    assert result.oi_intensity == 0.0
