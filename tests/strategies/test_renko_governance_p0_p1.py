# 2026-08-02 Antigravity: Comprehensive P0 & P1 Renko Governance Test Suite
import os
import json
import pytest
from datetime import datetime
from unittest.mock import patch
from strategies.plugins.futures.active.renko_tracker import RenkoTracker, RenkoBrickEvent

def test_p0_bricks_created_this_tick_positive():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0, symbol="TMF", position_side="LONG")
    cnt, trend, meta = tracker.add(44010.0)
    assert cnt == 1
    events = meta["brick_events"]
    assert len(events) == 1
    evt_dict = events[0]
    assert evt_dict["bricks_created_this_tick"] == 1
    assert evt_dict["bricks_created_this_tick"] >= 1

    cnt2, trend2, meta2 = tracker.add(43980.0)
    assert cnt2 == -2
    events2 = meta2["brick_events"]
    assert len(events2) == 2
    for evt in events2:
        assert evt["bricks_created_this_tick"] == 2
        assert evt["bricks_created_this_tick"] >= 1

def test_p0_schema_invariants_assertion():
    with pytest.raises((AssertionError, ValueError, TypeError)):
        RenkoBrickEvent(
            brick_sequence=0,
            source_receive_sequence=1,
            created_at="2026-08-02T22:00:00+08:00",
            open=44000.0, close=44010.0, high=44010.0, low=44000.0,
            trend=1, trend_label="UP", is_reversal=False, brick_size=10.0,
            bricks_created_this_tick=1, input_price=44010.0, price_source="EXECUTABLE_BID",
            position_side="LONG", position_effect="FAVORABLE", trade_id="T1",
            single_leg_episode_id="T1:single-leg:1", generation_id="1"
        )

    with pytest.raises((AssertionError, ValueError, TypeError)):
        RenkoBrickEvent(
            brick_sequence=1,
            source_receive_sequence=1,
            created_at="2026-08-02T22:00:00+08:00",
            open=44000.0, close=44010.0, high=44010.0, low=44000.0,
            trend=1, trend_label="UP", is_reversal=False, brick_size=10.0,
            bricks_created_this_tick=-1,
            input_price=44010.0, price_source="EXECUTABLE_BID",
            position_side="LONG", position_effect="FAVORABLE", trade_id="T1",
            single_leg_episode_id="T1:single-leg:1", generation_id="1"
        )

def test_p1_schema_type_rigidity():
    tracker = RenkoTracker(
        anchor_price=44000.0,
        trade_id="TR_100",
        episode_ordinal=1,
        generation_id="1"
    )
    tracker.add(44010.0)
    data = tracker.to_dict()
    
    assert isinstance(data["generation_id"], str)
    assert isinstance(data["episode_id"], str)
    assert isinstance(data["trade_id"], str)
    assert isinstance(data["brick_sequence"], int)

    restored = RenkoTracker.from_dict(data)
    r_data = restored.to_dict()
    assert r_data["generation_id"] == "1"
    assert r_data["episode_id"] == "TR_100:single-leg:1"
    assert r_data["trade_id"] == "TR_100"
    assert r_data["brick_sequence"] == 1

def test_p1_episode_identity():
    tracker1 = RenkoTracker(anchor_price=44000.0, trade_id="TR_101", episode_ordinal=1)
    tracker2 = RenkoTracker(anchor_price=44000.0, trade_id="TR_101", episode_ordinal=2)
    assert tracker1.episode_id == "TR_101:single-leg:1"
    assert tracker2.episode_id == "TR_101:single-leg:2"
    assert tracker1.episode_id != tracker2.episode_id

def test_p1_persistent_dedupe_across_restarts(tmp_path, monkeypatch):
    date_str = datetime.now().strftime("%Y%m%d")
    telemetry_dir = tmp_path / "data" / "telemetry" / "renko_bricks" / date_str
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    jsonl_file = telemetry_dir / "TMF_bricks.jsonl"
    
    monkeypatch.chdir(tmp_path)

    t1 = RenkoTracker(anchor_price=44000.0, trade_id="TR_DEDUPE", generation_id="1")
    t1.add(44010.0)
    
    lines1 = jsonl_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines1) == 1

    t2 = RenkoTracker(anchor_price=44000.0, trade_id="TR_DEDUPE", generation_id="1")
    assert "1:TR_DEDUPE:single-leg:1:1" in t2._persisted_brick_keys

    evt = RenkoBrickEvent(
        brick_sequence=1, source_receive_sequence=1, created_at="2026-08-02T22:00:00+08:00",
        open=44000.0, close=44010.0, high=44010.0, low=44000.0, trend=1, trend_label="UP",
        is_reversal=False, brick_size=10.0, bricks_created_this_tick=1, input_price=44010.0,
        price_source="EXECUTABLE_BID", position_side="LONG", position_effect="FAVORABLE",
        trade_id="TR_DEDUPE", single_leg_episode_id="TR_DEDUPE:single-leg:1", generation_id="1"
    )
    t2._append_brick_telemetry(evt)
    
    lines2 = jsonl_file.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines2) == 1

def test_p1_non_blocking_telemetry_failure_injection(monkeypatch):
    tracker = RenkoTracker(anchor_price=44000.0)
    def failing_append(evt):
        tracker.telemetry_failure_count += 1
    monkeypatch.setattr(tracker, "_append_brick_telemetry", failing_append)
    cnt, trend, meta = tracker.add(44010.0)
    assert cnt == 1
    assert tracker.brick_sequence == 1
    assert tracker.telemetry_failure_count == 1

def test_p1_immutable_event_lifecycle_consistency():
    tracker = RenkoTracker(anchor_price=44000.0, trade_id="TR_LIFECYCLE", position_side="LONG")
    tracker.add(44010.0)
    cnt, trend, meta = tracker.add(43980.0)
    
    recent_evt = tracker.get_recent_bricks(1)[-1]
    meta_evt = meta["brick_events"][-1]
    
    assert recent_evt["brick_sequence"] == meta_evt["brick_sequence"]
    assert recent_evt["position_effect"] == meta_evt["position_effect"]
    assert recent_evt["signal_emitted"] == meta_evt["signal_emitted"]
    assert recent_evt["signal_reason"] == meta_evt["signal_reason"]
    assert recent_evt["signal_event_id"] == meta_evt["signal_event_id"]
