# 2026-08-02 Antigravity: Test Suite for Renko Requirements 1, 3, 4, 5

import os
import json
import pytest
from datetime import datetime, timezone, timedelta
from strategies.plugins.futures.active.renko_tracker import RenkoTracker, RenkoBrickEvent

def test_req1_explicit_exception_invariants():
    with pytest.raises(TypeError):
        RenkoBrickEvent(
            brick_sequence=1, created_at="2026-08-02T22:00:00+08:00",
            open=44000.0, close=44010.0, high=44010.0, low=44000.0, trend=1, trend_label="UP",
            is_reversal=False, brick_size=10.0, bricks_created_this_tick=True,
            input_price=44010.0, price_source="EXECUTABLE_BID", position_side="LONG", position_effect="FAVORABLE",
            trade_id="T1", single_leg_episode_id="T1:single-leg:1", generation_id="1"
        )
    with pytest.raises(ValueError):
        RenkoBrickEvent(
            brick_sequence=1, created_at="2026-08-02T22:00:00+08:00",
            open=44000.0, close=44010.0, high=44010.0, low=44000.0, trend=1, trend_label="UP",
            is_reversal=False, brick_size=10.0, bricks_created_this_tick=0,
            input_price=44010.0, price_source="EXECUTABLE_BID", position_side="LONG", position_effect="FAVORABLE",
            trade_id="T1", single_leg_episode_id="T1:single-leg:1", generation_id="1"
        )
    with pytest.raises(ValueError):
        RenkoBrickEvent(
            brick_sequence=1, created_at="2026-08-02T22:00:00+08:00",
            open=44000.0, close=44010.0, high=44010.0, low=44000.0, trend=0, trend_label="UP",
            is_reversal=False, brick_size=10.0, bricks_created_this_tick=1,
            input_price=44010.0, price_source="EXECUTABLE_BID", position_side="LONG", position_effect="FAVORABLE",
            trade_id="T1", single_leg_episode_id="T1:single-leg:1", generation_id="1"
        )
    with pytest.raises(ValueError):
        RenkoBrickEvent(
            brick_sequence=1, created_at="2026-08-02T22:00:00+08:00",
            open=44000.0, close=44000.0, high=44000.0, low=44000.0, trend=1, trend_label="UP",
            is_reversal=False, brick_size=10.0, bricks_created_this_tick=1,
            input_price=44000.0, price_source="EXECUTABLE_BID", position_side="LONG", position_effect="FAVORABLE",
            trade_id="T1", single_leg_episode_id="T1:single-leg:1", generation_id="1"
        )

def test_req3_dedupe_edge_cases_and_counters(tmp_path, monkeypatch):
    date_str = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    telemetry_dir = tmp_path / "data" / "telemetry" / "renko_bricks" / date_str
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    jsonl_file = telemetry_dir / "TMF_bricks.jsonl"
    line1 = json.dumps({"generation_id":"1", "single_leg_episode_id":"EP_1:single-leg:1", "brick_sequence":1})
    line2 = "MALFORMED_JSON_LINE"
    line3 = json.dumps({"generation_id":"1", "single_leg_episode_id":"EP_1:single-leg:1", "brick_sequence":2})
    line4 = '{"generation_id":"1", "single_leg_episode_id="P_1", "brick_sequence":'
    content = line1 + "\n" + line2 + "\n" + line3 + "\n" + line4
    jsonl_file.write_text(content, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    tracker = RenkoTracker(anchor_price=44000.0, trade_id="EP_1", generation_id="1")
    assert tracker.dedupe_keys_loaded == 2
    assert tracker.malformed_lines_skipped == 2
    assert tracker.dedupe_load_duration_ms >= 0.0
    assert "1:EP_1:single-leg:1:1" in tracker._persisted_brick_keys
    assert "1:EP_1:single-leg:1:2" in tracker._persisted_brick_keys

def test_req4_source_receive_sequence_contract():
    evt1 = RenkoBrickEvent(
        brick_sequence=1, created_at="2026-08-02T22:00:00+08:00",
        open=44000.0, close=44010.0, high=44010.0, low=44000.0, trend=1, trend_label="UP",
        is_reversal=False, brick_size=10.0, bricks_created_this_tick=1,
        input_price=44010.0, price_source="EXECUTABLE_BID", position_side="LONG", position_effect="FAVORABLE",
        trade_id="T1", single_leg_episode_id="T1:single-leg:1", generation_id="1",
        source_receive_sequence=105
    )
    assert evt1.source_receive_sequence == 105
    evt2 = RenkoBrickEvent(
        brick_sequence=1, created_at="2026-08-02T22:00:00+08:00",
        open=44000.0, close=44010.0, high=44010.0, low=44000.0, trend=1, trend_label="UP",
        is_reversal=False, brick_size=10.0, bricks_created_this_tick=1,
        input_price=44010.0, price_source="EXECUTABLE_BID", position_side="LONG", position_effect="FAVORABLE",
        trade_id="T1", single_leg_episode_id="T1:single-leg:1", generation_id="1",
        source_receive_sequence=None
    )
    assert evt2.source_receive_sequence is None
    with pytest.raises(ValueError):
        RenkoBrickEvent(
            brick_sequence=1, created_at="2026-08-02T22:00:00+08:00",
            open=44000.0, close=44010.0, high=44010.0, low=44000.0, trend=1, trend_label="UP",
            is_reversal=False, brick_size=10.0, bricks_created_this_tick=1,
            input_price=44010.0, price_source="EXECUTABLE_BID", position_side="LONG", position_effect="FAVORABLE",
            trade_id="T1", single_leg_episode_id="T1:single-leg:1", generation_id="1",
            source_receive_sequence=-1
        )

def test_req5_flat_spread_state_transition_contract():
    tracker = RenkoTracker(anchor_price=44000.0, trade_id="TR_TRANSITION")
    tracker.add(44010.0)
    state_single_leg = {
        "position_phase": "SINGLE_LEG",
        "state_revision": 1,
        "renko_status": tracker.to_dict()
    }
    assert state_single_leg["renko_status"] is not None
    assert len(state_single_leg["renko_status"]['recent_bricks']) == 1

    state_flat = {
        "position_phase": "FLAT",
        "state_revision": 2,
        "renko_status": None
    }
    assert state_flat["renko_status"] is None

    state_spread = {
        "position_phase": "SPREAD",
        "state_revision": 3,
        "renko_status": None
    }
    assert state_spread["renko_status"] is None
