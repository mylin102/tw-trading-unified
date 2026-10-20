# 2026-10-15 Hermes Agent: Task 1.3.2 optional quality integration contract.
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
from strategies.plugins.futures.active.mts_oi_estimator import OISnapshot
from strategies.plugins.futures.active.mts_vol_quality import VolQuality


TW = timezone(timedelta(hours=8))
TS = datetime(2026, 10, 15, 10, 0, tzinfo=TW).timestamp()
# 2026-10-20 Hermes Agent: derive the serialized Gaussian-quality expectation from the artifact.
VOLUME_PARAMS = json.loads((Path(__file__).parents[1] / "config/mts_vol_zscore_params.json").read_text())["params"]


def calibrated_quality(volume, segment):
    params = VOLUME_PARAMS[segment]
    z = (volume - params["mean"]) / params["std"] if params["std"] else 0.0
    return 100 * math.exp(-(z ** 2) / 2)


def candidate(**kwargs):
    return evaluate_hvwap_candidate(
        decision_ts=TS,
        near_bars=[{"ts": TS, "close": 100, "volume": 467}],
        far_bars=[{"ts": TS, "close": 90, "volume": 210}],
        near_price=100, far_price=90, near_side="LONG", far_side="SHORT",
        atr_15m=10, **kwargs)


def snapshot(ts=TS):
    return OISnapshot(ts, 1.0, 2.0, 0.9, 10, 0, 9, 98.2)


def test_new_fields_are_optional_and_default_none():
    names = {"vol_quality_score", "vol_quality_level"}
    selected = [f for f in fields(HvwapCandidateVerdict) if f.name in names]
    assert len(selected) == 2
    assert all(f.default is None and "Optional[" in f.type for f in selected)
    data = candidate().to_dict()
    assert data["vol_quality_score"] is None
    assert data["vol_quality_level"] is None


def test_three_layers_serialize_without_changing_arbitration():
    oi = snapshot()
    baseline = candidate(oi_snapshot=oi)
    events = [{"order_id": "A", "side": "buy"}, {"order_id": "B", "side": "sell"}]
    result = candidate(oi_snapshot=oi, event_sequence=events)
    assert result.vol_quality_score == 100
    assert result.vol_quality_level == "EXCELLENT"
    assert result.vol_quality == pytest.approx(calibrated_quality(467, "day"))  # (P) JSON-derived Gaussian quality.
    assert result.oi_quality == 98.2
    assert replace(result, vol_quality_score=None, vol_quality_level=None) == baseline
    assert result.to_dict()["vol_quality_level"] == "EXCELLENT"


def test_all_five_indicators_are_computed(monkeypatch):
    called = []
    names = ["_compute_order_stacking", "_compute_event_density",
             "_compute_time_clustering", "_compute_bid_ask_imbalance",
             "_compute_follow_through"]
    for name in names:
        original = getattr(VolQuality, name)
        def observe(*args, _name=name, _original=original):
            called.append(_name)
            return _original(*args)
        monkeypatch.setattr(VolQuality, name, staticmethod(observe))
    result = candidate(oi_snapshot=snapshot(), event_sequence=[])
    assert result.vol_quality_score is not None
    assert called == names


def test_unavailable_and_future_inputs_remain_optional():
    assert candidate(oi_snapshot=snapshot()).vol_quality_score is None
    assert candidate(event_sequence=[]).vol_quality_score is None
    assert candidate(oi_snapshot=snapshot(TS + 1), event_sequence=[]).vol_quality_score is None


def test_quality_failure_preserves_oi_vwap_and_original_verdict(monkeypatch, caplog):
    oi = snapshot()
    baseline = candidate(oi_snapshot=oi)
    def fail(**kwargs):
        raise ValueError("quality unavailable")
    monkeypatch.setattr(VolQuality, "compute", fail)
    assert candidate(oi_snapshot=oi, event_sequence=[]) == baseline
    assert "volume quality unavailable" in caplog.text


def test_strategy_passes_exact_completed_window_and_counts(monkeypatch, tmp_path):
    from strategies.plugins.futures.active import tmf_spread as mts
    path = tmp_path / "mode-scoped-events.jsonl"
    events = [
        {"event": "ENTRY", "order_id": "OLD", "ts": "2026-10-15T09:55:00+08:00"},
        {"event": "ENTRY", "order_id": "A", "ts": "2026-10-15T09:56:00+08:00"},
        {"event": "ENTRY_AUDIT", "order_id": "A", "ts": "2026-10-15T09:57:00+08:00"},
        {"event": "EXIT_LOG", "order_id": "B", "ts": "2026-10-15T09:58:00+08:00"},
        {"event": "LEG_FILLED", "order_id": "A", "ts": "2026-10-15T10:00:00+08:00"},
        {"event": "ENTRY", "order_id": "FUTURE", "ts": "2026-10-15T10:00:01+08:00"},
        {"event": "ENTRY", "order_id": "BAD", "ts": "invalid"},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    monkeypatch.setattr(mts, "_MTS_EVENT_LOG", str(path))
    monkeypatch.setenv("MTS_EVENT_LOG_PATH", str(tmp_path / "wrong-mode.jsonl"))
    emitted = []
    monkeypatch.setattr(mts, "_append_event", lambda name, **payload: emitted.append((name, payload)))
    obj = mts.TMFSpread.__new__(mts.TMFSpread)
    obj._hvwap_5m_bars = deque([{"ts": TS, "close": 100, "volume": 467,
                                "far_close": 90, "far_volume": 210}])
    obj._has_position = True
    obj._near_side, obj._far_side = "LONG", "SHORT"
    obj._build_trend_release_input = lambda: (False, None)
    observed = []
    compute = VolQuality.compute
    def capture(**kwargs):
        observed.append(kwargs)
        return compute(**kwargs)
    monkeypatch.setattr(VolQuality, "compute", capture)
    bar = {"ts": TS, "close": 100, "far_close": 90, "volume": 9999, "atr": 10}
    obj._hvwap_candidate_tick(bar, datetime.fromtimestamp(TS, TW))
    assert observed[0] == {"ts": TS, "order_count": 2, "fill_count": 1,
                           "event_sequence": events[1:5]}
    name, payload = emitted[0]
    assert name == "HVWAP_CANDIDATE"
    assert payload["vol_quality_score"] == compute(**observed[0]).quality_score
    assert payload["vol_quality_level"] == compute(**observed[0]).quality_level
    assert payload["oi_intensity"] == 0.4
    assert payload["vol_zscore"] == pytest.approx((467 - VOLUME_PARAMS["day"]["mean"]) / VOLUME_PARAMS["day"]["std"])
    assert payload["release_action_emitted"] is False
    # Same-bar cache must not ingest a newly appended late/future event.
    monkeypatch.setattr(obj._oi_estimator, "estimate_oi", lambda *args: pytest.fail("same-bar reload"))
    obj._hvwap_candidate_tick(bar, datetime.fromtimestamp(TS, TW))
    assert observed[1] == observed[0]
    assert len(emitted) == 1


def test_missing_log_does_not_fabricate_quality(monkeypatch, tmp_path):
    from strategies.plugins.futures.active import tmf_spread as mts
    monkeypatch.setattr(mts, "_MTS_EVENT_LOG", str(tmp_path / "missing.jsonl"))
    monkeypatch.setattr(mts, "_append_event", lambda *args, **kwargs: None)
    obj = mts.TMFSpread.__new__(mts.TMFSpread)
    obj._hvwap_5m_bars = deque([{"ts": TS, "close": 100, "volume": 467,
                                "far_close": 90, "far_volume": 210}])
    obj._has_position = True
    obj._near_side, obj._far_side = "LONG", "SHORT"
    obj._build_trend_release_input = lambda: (False, None)
    obj._hvwap_candidate_tick({"ts": TS, "close": 100, "far_close": 90, "atr": 10},
                              datetime.fromtimestamp(TS, TW))
    assert obj._hvwap_last_verdict.vol_quality_score is None
    assert obj._hvwap_last_verdict.vol_quality_level is None
