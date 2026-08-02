# 2026-08-02 Antigravity: Unit tests for RenkoTracker
import pytest
from strategies.plugins.futures.active.renko_tracker import RenkoTracker

def test_renko_initialization():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0)
    assert tracker.renko_open == 44000.0
    assert tracker.renko_close == 44000.0
    assert tracker.trend == 0
    assert tracker.total_bricks == 0


def test_renko_initial_bricks_upward():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0)
    num_bricks, trend = tracker.add(44012.0)
    assert num_bricks == 1
    assert trend == 1
    assert tracker.renko_open == 44000.0
    assert tracker.renko_close == 44010.0


def test_renko_noise_filtering():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0)
    tracker.add(44015.0)  # 1 brick up (44000 -> 44010)
    
    # Noise: price fluctuates between 44002 and 44018
    num_bricks, trend = tracker.add(44018.0)
    assert num_bricks == 0
    assert tracker.renko_close == 44010.0
    
    num_bricks, trend = tracker.add(44002.0)  # Drop to 44002 (above reversal threshold 43990)
    assert num_bricks == 0
    assert tracker.renko_close == 44010.0
    assert tracker.trend == 1  # Trend remains bullish!


def test_renko_bullish_continuation():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0)
    tracker.add(44010.0)  # 1 brick (44000 -> 44010)
    
    num_bricks, trend = tracker.add(44032.0)
    assert num_bricks == 2
    assert trend == 1
    assert tracker.renko_open == 44020.0
    assert tracker.renko_close == 44030.0


def test_renko_2brick_reversal():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0)
    tracker.add(44010.0)  # 1 bullish brick (peak close 44010)
    
    # Drop 20 points to 43989 (requires 2 x brick_size from peak close 44010)
    num_bricks, trend = tracker.add(43989.0)
    assert num_bricks == -1  # 1 bearish brick formed (open 44000, close 43990)
    assert trend == -1      # Trend reversed to Bearish!
    assert tracker.renko_open == 44000.0
    assert tracker.renko_close == 43990.0


def test_renko_dynamic_atr_update():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0)
    tracker.update_brick_size(atr=25.0, multiplier=0.5, min_floor=2.0)
    assert tracker.brick_size == 12.5


def test_renko_zero_or_none_safety():
    tracker = RenkoTracker(anchor_price=44000.0, brick_size=10.0)
    num_bricks, trend = tracker.add(0.0)
    assert num_bricks == 0
    num_bricks, trend = tracker.add(None)
    assert num_bricks == 0
