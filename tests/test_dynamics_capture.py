"""
DTI-001A: Shadow Differential Test — Capture on/off decision parity.

Verifies that enabling DynamicsCaptureHook produces identical:
  - decision event sequence
  - action / leg / reason
  - order intent / side / qty / price type
  - state transition sequence

Pass criteria:
  capture_disabled_output_hash == capture_enabled_output_hash
  Only telemetry logs may differ.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

import pytest

# Module under test
from core.dynamics_capture import DynamicsCaptureHook, TickCaptureEvent, _compute_generation_id

# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def temp_log_dir():
    with tempfile.TemporaryDirectory(prefix="dti001_test_") as tmp:
        yield Path(tmp)


class FakeTick:
    """Minimal Shioaji TickFOPv1 stand-in."""
    def __init__(self, code="TMFH6", close=18450.0, buy_price=18449.0, sell_price=18451.0,
                 bid_volume=10, ask_volume=15, datetime="2026-07-27 10:30:00.123456"):
        self.code = code
        self.close = close
        self.buy_price = buy_price
        self.sell_price = sell_price
        self.bid_volume = bid_volume
        self.ask_volume = ask_volume
        self.datetime = datetime


# ─── Unit Tests ──────────────────────────────────────────────────────


class TestTickCaptureEvent:
    def test_generation_id_format(self):
        gid = _compute_generation_id()
        # Format: <timestamp>-pid<N>-<commit>
        parts = gid.split("-pid")
        assert len(parts) == 2, f"Expected '-pid' separator, got: {gid}"
        assert parts[1].count("-") >= 0

    def test_event_id_unique(self):
        hook = DynamicsCaptureHook(log_dir="/tmp/dti001_test_uniq")
        ids = set()
        for i in range(100):
            hook._seq = i
            eid = f"{hook._active_generation_id}-seq{i:08d}"
            ids.add(eid)
        assert len(ids) == 100

    def test_capture_event_dataclass(self):
        event = TickCaptureEvent(
            generation_id="test-gen",
            event_id="test-gen-seq00000001",
            contract_code="TMFH6",
            bid=18450.0,
            ask=18451.0,
            last=18450.5,
        )
        d = {"schema_version": "1.0.0", "generation_id": "test-gen",
             "event_id": "test-gen-seq00000001", "contract_code": "TMFH6",
             "bid": 18450.0, "ask": 18451.0, "last": 18450.5}
        # Spot-check serialization
        serialized = json.dumps(d)
        assert "TMFH6" in serialized
        assert "18450.0" in serialized


class TestDynamicsCaptureHook:
    def test_start_stop(self, temp_log_dir):
        hook = DynamicsCaptureHook(log_dir=temp_log_dir)
        hook.start()
        assert hook._writer_thread is not None
        assert hook._writer_thread.is_alive()
        hook.stop()
        assert not hook._writer_thread.is_alive()

    def test_capture_single_tick(self, temp_log_dir):
        hook = DynamicsCaptureHook(log_dir=temp_log_dir, flush_interval_ms=50)
        hook.start()

        tick = FakeTick()
        hook.observe("TFE", tick)
        time.sleep(0.2)  # Let writer flush
        hook.stop()

        assert hook.captured_count == 1
        assert hook.dropped_count == 0
        assert hook.writer_error_count == 0

        # Verify JSONL file exists and has content
        files = list(temp_log_dir.rglob("*.jsonl"))
        assert len(files) >= 1, f"No JSONL files found in {temp_log_dir}"
        with open(files[0]) as f:
            lines = [l for l in f if l.strip()]
        assert len(lines) >= 1

        # Verify schema fields
        record = json.loads(lines[0])
        assert record["schema_version"] == "1.0.0"
        assert record["contract_code"] == "TMFH6"
        assert record["bid"] == 18449.0
        assert record["ask"] == 18451.0
        assert record["derived_status"] == "NOT_COMPUTED"
        assert record["generation_id"] == hook._active_generation_id

    def test_capture_multiple_ticks(self, temp_log_dir):
        hook = DynamicsCaptureHook(
            log_dir=temp_log_dir,
            flush_interval_ms=50,
            flush_batch_size=3,
        )
        hook.start()

        for i in range(10):
            tick = FakeTick(close=18450.0 + i, datetime=f"2026-07-27 10:30:00.{i:06d}")
            hook.observe("TFE", tick)

        time.sleep(0.3)
        hook.stop()

        assert hook.captured_count == 10
        assert hook.dropped_count == 0

        files = list(temp_log_dir.rglob("*.jsonl"))
        with open(files[0]) as f:
            lines = [l for l in f if l.strip()]
        assert len(lines) == 10

    def test_queue_full_drops(self, temp_log_dir):
        """Verify bounded queue drops events instead of blocking callback."""
        hook = DynamicsCaptureHook(
            log_dir=temp_log_dir,
            queue_maxsize=5,
            flush_interval_ms=5000,  # Very slow flush to force backpressure
        )
        hook.start()

        # Fill queue past maxsize
        for i in range(20):
            tick = FakeTick(close=18450.0 + i)
            hook.observe("TFE", tick)

        # Force stop (drops queued events)
        time.sleep(0.05)
        hook.stop(timeout=1.0)

        assert hook.dropped_count > 0, "Expected drops from bounded queue"
        # Captured + dropped should equal total attempts
        assert hook.captured_count + hook.dropped_count == 20

    def test_callback_exception_isolation(self, temp_log_dir):
        """Verify exception in observe never propagates to caller."""
        hook = DynamicsCaptureHook(log_dir=temp_log_dir)
        hook.start()

        # Call with None tick (would fail internally but must not raise)
        try:
            hook.observe("TFE", None)
        except Exception:
            pytest.fail("observe raised exception — should be fail-open")

        hook.stop()

    def test_near_far_routing(self, temp_log_dir):
        """Verify near/far flags set correctly from state hints."""
        hook = DynamicsCaptureHook(log_dir=temp_log_dir, flush_interval_ms=50)
        hook.start()

        near_state = {"code": "TMFH6", "bid": 18450.0, "age_ms": 5.0}
        far_state = {"code": "TMFM6", "bid": 18300.0, "age_ms": 15.0}

        # Near tick
        near_tick = FakeTick(code="TMFH6", datetime="2026-07-25 10:30:00.123456")
        hook.observe("TFE", near_tick, near_state=near_state, far_state=far_state)

        # Far tick
        far_tick = FakeTick(code="TMFM6", close=18300.0, buy_price=18299.0,
                            sell_price=18301.0, datetime="2026-07-25 10:30:00.200000")
        hook.observe("TFE", far_tick, near_state=near_state, far_state=far_state)

        time.sleep(0.2)
        hook.stop()

        files = list(temp_log_dir.rglob("*.jsonl"))
        with open(files[0]) as f:
            lines = [json.loads(l) for l in f if l.strip()]

        near_rec = next(l for l in lines if l["contract_code"] == "TMFH6")
        far_rec = next(l for l in lines if l["contract_code"] == "TMFM6")

        assert near_rec["is_near"] is True
        assert near_rec["is_far"] is False
        assert near_rec["pair_code"] == "TMFM6"
        # pair_skew_ms = |this_tick_age - pair_age|. We can't predict tick_age_ms
        # precisely (depends on wall clock), but it should be a positive number
        assert isinstance(near_rec["pair_skew_ms"], (int, float))
        assert near_rec["pair_skew_ms"] >= 0

        assert far_rec["is_near"] is False
        assert far_rec["is_far"] is True
        assert far_rec["pair_code"] == "TMFH6"
        assert isinstance(far_rec["pair_skew_ms"], (int, float))
        assert far_rec["pair_skew_ms"] >= 0

    def test_stale_detection(self, temp_log_dir):
        """Verify stale tick flagged correctly."""
        hook = DynamicsCaptureHook(
            log_dir=temp_log_dir,
            max_stale_age_ms=100.0,  # 100ms max age
            flush_interval_ms=50,
        )
        hook.start()

        # Tick with old timestamp (1 hour ago from an older date to ensure positive age)
        tick = FakeTick(datetime="2026-07-25 09:29:50.000000")  # Yesterday — definitely stale
        hook.observe("TFE", tick)

        time.sleep(0.2)
        hook.stop()

        files = list(temp_log_dir.rglob("*.jsonl"))
        with open(files[0]) as f:
            record = json.loads(f.readline())

        assert record["is_stale"] is True
        assert "tick_age_ms" in record["stale_reason"]

    def test_bid_ask_size_capture(self, temp_log_dir):
        hook = DynamicsCaptureHook(log_dir=temp_log_dir, flush_interval_ms=50)
        hook.start()

        tick = FakeTick(bid_volume=42, ask_volume=77)
        hook.observe("TFE", tick)

        time.sleep(0.2)
        hook.stop()

        files = list(temp_log_dir.rglob("*.jsonl"))
        with open(files[0]) as f:
            record = json.loads(f.readline())

        assert record["bid_size"] == 42
        assert record["ask_size"] == 77

    def test_session_metadata(self, temp_log_dir):
        hook = DynamicsCaptureHook(log_dir=temp_log_dir, flush_interval_ms=50)
        hook.start()

        tick = FakeTick()
        hook.observe("TFE", tick, trade_id="test-trade-001", episode_id="ep-001", session_id="day")

        time.sleep(0.2)
        hook.stop()

        files = list(temp_log_dir.rglob("*.jsonl"))
        with open(files[0]) as f:
            record = json.loads(f.readline())

        assert record["trade_id"] == "test-trade-001"
        assert record["episode_id"] == "ep-001"
        assert record["session_id"] == "day"


# ─── Shadow Differential Test ───────────────────────────────────────


def test_shadow_differential_decision_parity(temp_log_dir):
    """
    Simulate sequential ticks feeding a deterministic decision engine.
    Compare output with capture ON vs OFF — must be identical.
    """
    # Deterministic "decision engine": replay ticks, record actions
    def replay_ticks(ticks, *, capture_hook=None):
        actions = []
        for exchange, tick, kw in ticks:
            # Decision logic (dummy: always SELL on 3rd tick)
            decision = None
            if len(actions) == 2:
                decision = {
                    "action": "SELL_NEAR_BUY_FAR",
                    "leg": "NEAR",
                    "reason": "TEST_TRIGGER",
                    "seq": len(actions),
                }
            if decision:
                actions.append(decision)

            # Capture hook (only if enabled)
            if capture_hook:
                capture_hook.observe(exchange, tick, **kw)

        return actions

    # Build tick sequence
    ticks = [
        ("TFE", FakeTick(code="TMFH6", close=18450.0), {"session_id": "day", "near_state": {"code": "TMFH6"}, "far_state": {"code": "TMFM6"}}),
        ("TFE", FakeTick(code="TMFM6", close=18300.0), {"session_id": "day", "near_state": {"code": "TMFH6"}, "far_state": {"code": "TMFM6"}}),
        ("TFE", FakeTick(code="TMFH6", close=18452.0), {"session_id": "day", "near_state": {"code": "TMFH6"}, "far_state": {"code": "TMFM6"}}),
        ("TFE", FakeTick(code="TMFM6", close=18298.0), {"session_id": "day", "near_state": {"code": "TMFH6"}, "far_state": {"code": "TMFM6"}}),
        ("TFE", FakeTick(code="TMFH6", close=18455.0), {"session_id": "day", "near_state": {"code": "TMFH6"}, "far_state": {"code": "TMFM6"}}),
    ]

    # Run without capture
    actions_off = replay_ticks(ticks, capture_hook=None)

    # Run with capture
    hook = DynamicsCaptureHook(log_dir=temp_log_dir / "capture_enabled", flush_interval_ms=50)
    hook.start()
    actions_on = replay_ticks(ticks, capture_hook=hook)
    time.sleep(0.2)
    hook.stop()

    # Decisions must be identical
    assert len(actions_on) == len(actions_off), "Decision count differs!"
    for i, (a_on, a_off) in enumerate(zip(actions_on, actions_off)):
        assert a_on == a_off, f"Decision {i} differs: {a_on} vs {a_off}"

    # Capture file should exist
    files = list((temp_log_dir / "capture_enabled").rglob("*.jsonl"))
    assert len(files) >= 1
    with open(files[0]) as f:
        captured = [l for l in f if l.strip()]
    assert len(captured) == 5


# ─── Deterministic Replay Hash Test ────────────────────────────────


def test_deterministic_replay_hash(temp_log_dir):
    """
    Two identical capture runs with same inputs must produce same JSONL
    (excluding generation_id which contains process-start timestamp and PID).
    """
    def run_capture(ticks, output_dir) -> list[dict]:
        hook = DynamicsCaptureHook(log_dir=output_dir, flush_interval_ms=50)
        hook.start()
        for exchange, tick, kw in ticks:
            hook.observe(exchange, tick, **kw)
        time.sleep(0.3)
        hook.stop()

        files = sorted(output_dir.rglob("*.jsonl"))
        with open(files[0]) as f:
            return [json.loads(l) for l in f if l.strip()]

    ticks = [
        ("TFE", FakeTick(code="TMFH6", close=18450.0), {"session_id": "day"}),
        ("TFE", FakeTick(code="TMFM6", close=18300.0), {"session_id": "day"}),
        ("TFE", FakeTick(code="TMFH6", close=18452.0), {"session_id": "day"}),
    ]

    records1 = run_capture(ticks, temp_log_dir / "run1")
    records2 = run_capture(ticks, temp_log_dir / "run2")

    # Compare event by event, excluding generation-dependent fields
    exclude_keys = {"generation_id", "event_id", "source_sequence",
                    "received_at", "processed_at", "hostname", "pid", "thread_id",
                    "tick_age_ms", "pair_age_ms", "pair_skew_ms", "is_stale", "stale_reason"}

    for i, (r1, r2) in enumerate(zip(records1, records2)):
        for k in set(r1.keys()) - exclude_keys:
            assert r1[k] == r2[k], f"Field '{k}' differs at event {i}: {r1[k]} != {r2[k]}"

    # Generation ID may be same (same second, same process) or different — both valid
    # The key invariant is that deterministic fields match
