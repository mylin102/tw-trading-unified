# P2 contracts: Spread Synchronizer + Shadow Collector acceptance matrix.
# Shadow-only: no execution influence, no order submission, no live state mutation.
import json
import os
from pathlib import Path

import pytest

from core.spread_synchronizer import SpreadSample, SpreadSynchronizer
from core.spread_renko_shadow import SpreadRenkoShadowCollector


def _sync(**kw):
    defaults = dict(
        near_code="TMFH6", far_code="TMFI6",
        max_leg_age_ms=3000.0, max_pairing_skew_ms=2000.0,
        session_id="20260731_NIGHT",
    )
    defaults.update(kw)
    return SpreadSynchronizer(**defaults)


def _near(sync, price=43650.0, seq=1, ts=1000.0, code="TMFH6", session="20260731_NIGHT"):
    return dict(code=code, price=price, seq=seq, ts_ms=ts, session_id=session)


def _far(sync, price=43800.0, seq=1, ts=1200.0, code="TMFI6", session="20260731_NIGHT"):
    return dict(code=code, price=price, seq=seq, ts_ms=ts, session_id=session)


# ── pairing semantics ─────────────────────────────────────────────────────
def test_pairing_requires_same_contract_pair():
    s = _sync()
    s.on_near(_near(s, code="TMFH6"))
    sample = s.on_far(_far(s, code="TMFI7"))  # wrong far month
    assert sample is None, "cross-contract-pair must not pair"
    assert s.rejections[-1] == "CONTRACT_PAIR_MISMATCH"


def test_pairing_requires_same_session():
    s = _sync()
    s.on_near(_near(s, session="20260731_DAY"))
    sample = s.on_far(_far(s, session="20260731_NIGHT"))
    assert sample is None, "cross-session must not pair"


def test_sequence_monotonic_out_of_order_rejected():
    s = _sync()
    s.on_near(_near(s, seq=2, ts=2000.0))
    sample = s.on_near(_near(s, seq=1, ts=1500.0))  # older seq
    assert sample is None, "out-of-order near must not produce sample"
    assert s.rejections[-1] == "OUT_OF_ORDER_SEQUENCE"


def test_duplicate_tick_no_double_event():
    s = _sync()
    s.on_near(_near(s, seq=5, ts=1000.0))
    s.on_far(_far(s, seq=5, ts=1100.0))
    n_before = len(s.samples)
    s.on_near(_near(s, seq=5, ts=1000.0))  # replay same tick
    assert len(s.samples) == n_before, "duplicate tick must not create a new sample"


def test_stale_leg_no_sample():
    s = _sync(max_leg_age_ms=100.0)
    s.on_near(_near(s, ts=1000.0))
    sample = s.on_far(_far(s, ts=5000.0))  # far 4s newer than near 1s
    assert sample is None, "stale near leg must not produce sample"


def test_pair_skew_gate():
    s = _sync(max_pairing_skew_ms=500.0)
    s.on_near(_near(s, ts=1000.0))
    sample = s.on_far(_far(s, ts=3000.0))  # skew 2s > 500ms
    assert sample is None, "pair skew beyond gate must not produce sample"


def test_normal_pair_emits_sample():
    s = _sync()
    s.on_near(_near(s, price=43650.0, ts=1000.0))
    sample = s.on_far(_far(s, price=43800.0, ts=1100.0))
    assert sample is not None
    assert sample.spread_value == -150.0  # canonical spread = near - far
    assert sample.pairing_skew_ms == 100.0
    assert sample.near_contract == "TMFH6"
    assert sample.far_contract == "TMFI6"


# ── collector ─────────────────────────────────────────────────────────────
def test_collector_rejects_gated_ticks():
    """Collector only accepts ticks already passed Session→QuoteIntegrity→Jump."""
    c = SpreadRenkoShadowCollector()
    # rejected tick (simulated upstream rejection) must not enter collector
    with pytest.raises(ValueError):
        c.accept_tick(sample=None, source="near", rejected_reason="SESSION_REJECT")


def test_collector_exception_does_not_break_loop():
    c = SpreadRenkoShadowCollector()
    # simulate a bad sample dict → collector must log, not raise
    bad = {"spread_value": {"unserializable": object()}}  # genuinely un-serializable
    result = c.record(bad)
    assert result is False or result is None  # swallowed
    assert c.errors >= 1


def test_restart_sequence_does_not_repeat(tmp_path):
    path = tmp_path / "shadow.jsonl"
    c1 = SpreadRenkoShadowCollector(telemetry_path=str(path), process_instance_id="P1")
    c1.record({"spread_value": 150.0, "collector_sequence": 1})
    c1.record({"spread_value": 151.0, "collector_sequence": 2})
    c2 = SpreadRenkoShadowCollector(telemetry_path=str(path), process_instance_id="P2")
    c2.resume_from_disk()
    assert c2.collector_sequence == 2, "restart must resume after last written seq"


def test_telemetry_schema_fields(tmp_path):
    path = tmp_path / "shadow.jsonl"
    c = SpreadRenkoShadowCollector(telemetry_path=str(path), process_instance_id="P1")
    s = SpreadSample(
        schema_version=1, episode_id="EPI_1", trade_id="TR_1", session_id="20260731_NIGHT",
        near_contract="TMFH6", far_contract="TMFI6", near_sequence=1, far_sequence=1,
        near_timestamp=1000.0, far_timestamp=1100.0, pairing_skew_ms=100.0,
        spread_value=150.0, collector_sequence=1, process_instance_id="P1",
    )
    c.record(s)
    rows = [json.loads(l) for l in path.read_text().strip().splitlines()]
    assert rows[0]["schema_version"] == 1
    assert rows[0]["spread_value"] == 150.0  # sample built directly with 150
    assert rows[0]["collector_sequence"] == 1
    assert rows[0]["process_instance_id"] == "P1"


def test_shadow_does_not_mutate_live_renko_state(tmp_path):
    """Collector must never touch live Renko tracker / position / order state."""
    from strategies.plugins.futures.active.renko_tracker import RenkoTracker
    tracker = RenkoTracker(anchor_price=43650.0, brick_size=10.0)
    tracker.add(43660.0)
    snapshot = tracker.to_dict()

    c = SpreadRenkoShadowCollector(telemetry_path=str(tmp_path / "s.jsonl"))
    s = SpreadSample(schema_version=1, episode_id="E", trade_id=None, session_id="S",
                     near_contract="TMFH6", far_contract="TMFI6", near_sequence=1, far_sequence=1,
                     near_timestamp=1.0, far_timestamp=1.1, pairing_skew_ms=100.0,
                     spread_value=150.0, collector_sequence=1, process_instance_id="P")
    c.record(s)
    assert tracker.to_dict() == snapshot, "collector must not mutate live Renko state"
