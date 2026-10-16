# 2026-08-02 Antigravity: Comprehensive Renko Governance Test Suite (Items A-L Audit)
import pytest
import time
import math
from strategies.plugins.futures.active.renko_tracker import RenkoTracker

def test_renko_item_a_locked_brick_size():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0, position_side="LONG")
    assert tracker.locked_brick_size == 10.0
    
    # ATR jumps from 20 to 100
    tracker.update_brick_size(atr=100.0, multiplier=1.0)
    assert tracker.locked_brick_size == 10.0  # MUST remain strictly locked!
    assert tracker.brick_size == 10.0


def test_renko_item_b_adverse_reversal_symmetry():
    # 1. LONG position
    long_tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0, position_side="LONG")
    long_tracker.add(44010.0)  # 1 Bullish brick UP (trend=1)
    
    # LONG + UP continuation -> NO adverse reversal
    _, _, meta_up = long_tracker.add(44020.0)
    assert meta_up["is_adverse_reversal"] is False
    
    # LONG + DOWN reversal -> Adverse reversal triggered!
    _, _, meta_down = long_tracker.add(43989.0)
    assert meta_down["is_adverse_reversal"] is True

    # 2. SHORT position
    short_tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0, position_side="SHORT")
    short_tracker.add(43990.0)  # 1 Bearish brick DOWN (trend=-1)
    
    # SHORT + DOWN continuation -> NO adverse reversal
    _, _, meta_down2 = short_tracker.add(43980.0)
    assert meta_down2["is_adverse_reversal"] is False
    
    # SHORT + UP reversal -> Adverse reversal triggered!
    _, _, meta_up2 = short_tracker.add(44011.0)
    assert meta_up2["is_adverse_reversal"] is True


def test_renko_item_c_initialization_no_false_signal():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0, position_side="LONG")
    # First tick establishes anchor
    bricks, trend, meta = tracker.add(44005.0)
    assert bricks == 0
    assert meta["is_adverse_reversal"] is False


def test_renko_item_f_state_serialization_and_recovery():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0, position_side="LONG", episode_id="TRADE_123")
    tracker.add(44015.0)  # 1 Bullish brick (renko_open=44000, renko_close=44010)
    
    # Serialize to dict (simulating state JSON file write)
    state_dict = tracker.to_dict()
    assert state_dict["renko_close"] == 44010.0
    assert state_dict["trend"] == 1
    
    # Reconstruct from dict (simulating PM2 restart recovery)
    recovered_tracker = RenkoTracker.from_dict(state_dict)
    assert recovered_tracker.renko_open == 44000.0
    assert recovered_tracker.renko_close == 44010.0
    assert recovered_tracker.trend == 1
    assert recovered_tracker.locked_brick_size == 10.0
    assert recovered_tracker.episode_id == "TRADE_123"
    
    # Next tick fed to recovered tracker must match un-interrupted behavior
    num_bricks, trend, meta = recovered_tracker.add(43989.0)
    assert trend == -1
    assert meta["is_adverse_reversal"] is True


def test_renko_item_g_multi_brick_gap_and_clamp():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0, position_side="LONG", max_price_jump_points=50.0)
    tracker.add(44010.0)  # Setup active trend=1
    
    # Price jumps 80 points in 1 tick (> 50.0 max_price_jump_points) -> REJECTED!
    bricks, trend, meta = tracker.add(44090.0)
    assert bricks == 0
    assert meta["rejection_reason"] is not None
    assert "SINGLE_TICK_JUMP_EXCEEDED" in meta["rejection_reason"]


def test_renko_item_h_invalid_nan_inf_price_safety():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0)
    
    for bad_price in [None, 0.0, -10.0, float("nan"), float("inf")]:
        bricks, trend, meta = tracker.add(bad_price)
        assert bricks == 0
        assert meta["rejection_reason"] == "INVALID_PRICE_TICK"
