import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
import pandas as pd
from strategies.plugins.futures.active.tmf_spread import TMFSpread
from core.strategy_context import StrategyContext, MarketData, PositionView

@pytest.fixture
def strategy():
    s = TMFSpread()
    # Mock _restore_position_state to avoid interference from /tmp/mts_position_state.json
    s._restore_position_state = MagicMock(return_value=False)
    
    config = {
        "params": {
            "min_atr": 10.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 3.5,
            "release_stop_points": 20,
            "trail_distance_points": 30
        }
    }
    market = MarketData(last_bar={})
    position = PositionView(size=0)
    context = StrategyContext(market=market, position=position, config=config)
    s.init(context)
    return s

def test_atr_filter_too_low(strategy):
    bar = {
        "near_close": 41000.0,
        "far_close": 41100.0,
        "spread_z": -3.0,
        "atr": 5.0, # Less than min_atr 10.0
        "timestamp": datetime.now()
    }
    config = {
        "params": {
            "min_atr": 10.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 3.5,
            "release_stop_points": 20,
            "trail_distance_points": 30
        }
    }
    market = MarketData(last_bar=bar)
    position = PositionView(size=0)
    context = StrategyContext(market=market, position=position, config=config)
    
    # We need to mock _set_eval because it's a method of StrategyBase
    with patch.object(TMFSpread, '_set_eval') as mock_eval:
        signal = strategy.on_bar(context)
        assert signal is None
        mock_eval.assert_called_once()
        args, kwargs = mock_eval.call_args
        assert "ATR_TOO_LOW" in kwargs.get("skip_reason", "")

def test_atr_scaling_logic(strategy):
    bar = {"atr": 12.0}
    stop, trail = strategy._get_thresholds(bar)
    # 12.0 * 2.0 = 24.0
    # 12.0 * 3.5 = 42.0
    assert stop == 24.0
    assert trail == 42.0

def test_atr_floor_logic(strategy):
    bar = {"atr": 1.0}
    stop, trail = strategy._get_thresholds(bar)
    # 1.0 * 2.0 = 2.0 -> floor 10.0
    # 1.0 * 3.5 = 3.5 -> floor 20.0
    assert stop == 10.0
    assert trail == 20.0

def test_fixed_fallback_when_no_atr(strategy):
    bar = {"atr": None}
    stop, trail = strategy._get_thresholds(bar)
    assert stop == 20.0
    assert trail == 30.0

def test_on_bar_entry_with_atr(strategy):
    bar = {
        "near_close": 41000.0,
        "far_close": 41100.0,
        "spread_z": -3.0,
        "atr": 15.0, # Above min_atr 10.0
        "timestamp": datetime.now()
    }
    config = {
        "params": {
            "min_atr": 10.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 3.5,
            "release_stop_points": 20,
            "trail_distance_points": 30
        }
    }
    market = MarketData(last_bar=bar)
    position = PositionView(size=0)
    context = StrategyContext(market=market, position=position, config=config)
    
    signal = strategy.on_bar(context)
    assert signal is not None
    assert signal.action == "BUY_NEAR_SELL_FAR"
    
    # [GSD] Deferred Strategy Sync: has_position remains False until sync_position is called
    assert strategy._has_position is False
    assert strategy._lifecycle == "SUBMITTING"

    # Now simulate the fill sync
    strategy.sync_position("mts-test-123", "LONG", 41000.0, 41100.0)
    assert strategy._has_position is True
    assert strategy._lifecycle == "OPEN"

@patch("strategies.plugins.futures.active.tmf_spread._write_mts_state")
def test_mts_state_thresholds(mock_write, strategy):
    bar = {
        "near_close": 41000.0,
        "far_close": 41100.0,
        "spread_z": -3.0,
        "atr": 12.0, # stop=24, trail=42
        "timestamp": datetime.now()
    }
    config = {
        "params": {
            "min_atr": 10.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 3.5,
            "release_stop_points": 20,
            "trail_distance_points": 30
        }
    }
    market = MarketData(last_bar=bar)
    position = PositionView(size=0)
    context = StrategyContext(market=market, position=position, config=config)
    
    strategy.on_bar(context)
    
    # Verify _write_mts_state was called with dynamic thresholds
    mock_write.assert_called()
    # Find the call where release_stop_points is 24.0
    found = False
    for call in mock_write.call_args_list:
        if call.kwargs.get("release_stop_points") == 24.0 and call.kwargs.get("trail_distance_points") == 42.0:
            found = True
            break
    assert found, f"Dynamic thresholds (24.0, 42.0) not found in _write_mts_state calls: {mock_write.call_args_list}"


def test_risk_meta_generation(strategy):
    # 2026-06-26 Gemini CLI: verify ATR_DYNAMIC + FLOOR + FALLBACK_SOURCE + TICK_CONFIRMATION + QUOTE_FRESHNESS
    
    # Case A: With active ATR (ATR_DYNAMIC, TICK_CONFIRMATION, QUOTE_FRESHNESS)
    bar_with_atr = {
        "atr": 15.0,
        "near_tick_age_ms": 150.0,
        "far_tick_age_ms": 210.0,
        "confirm_ticks": 3
    }
    meta = strategy._get_risk_meta(bar_with_atr)
    assert meta["risk_mode"] == "ATR_DYNAMIC"
    assert meta["atr"] == 15.0
    assert meta["stop_mult"] == 2.0
    assert meta["trail_mult"] == 3.5
    assert meta["release_stop"] == 30.0 # 15.0 * 2.0
    assert meta["trail_dist"] == 52.5 # 15.0 * 3.5
    assert meta["release_stop_floor"] == 10.0
    assert meta["trail_dist_floor"] == 20.0
    assert meta["final_release_stop"] == 30.0
    assert meta["final_trail_dist"] == 52.5
    assert meta["quote_age_ms"] == 210.0
    assert meta["confirm_ticks"] == 3

    # Case B: With low ATR triggering floor limits (FLOOR)
    bar_low_atr = {
        "atr": 2.0,
        "near_tick_age_ms": 10.0,
        "far_tick_age_ms": 15.0,
    }
    meta_floor = strategy._get_risk_meta(bar_low_atr)
    assert meta_floor["risk_mode"] == "ATR_DYNAMIC"
    assert meta_floor["atr"] == 2.0
    assert meta_floor["release_stop"] == 4.0 # 2.0 * 2.0
    assert meta_floor["trail_dist"] == 7.0 # 2.0 * 3.5
    assert meta_floor["release_stop_floor"] == 10.0
    assert meta_floor["trail_dist_floor"] == 20.0
    assert meta_floor["final_release_stop"] == 10.0 # floored
    assert meta_floor["final_trail_dist"] == 20.0 # floored

    # Case C: Without ATR (FALLBACK_SOURCE, Default TICK_CONFIRMATION & QUOTE_FRESHNESS)
    bar_no_atr = {
        "atr": None,
        "near_tick_age_ms": -1,
        "far_tick_age_ms": -1,
    }
    strategy._last_atr = None
    meta_fallback = strategy._get_risk_meta(bar_no_atr)
    assert meta_fallback["risk_mode"] == "FIXED_FALLBACK"
    assert meta_fallback["atr"] == 0.0
    assert meta_fallback["release_stop"] == 20.0 # fallback stop
    assert meta_fallback["trail_dist"] == 30.0 # fallback trail
    assert meta_fallback["quote_age_ms"] == 0.0
    assert meta_fallback["confirm_ticks"] == 2 # default value

import time

def test_dynamic_entry_z(strategy):
    # Setup config dict for entry_z
    config = {
        "params": {
            "min_atr": 5.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 3.5,
            "release_stop_points": 20,
            "trail_distance_points": 30,
            "entry_z": {
                "low_atr": 2.0,
                "normal_atr": 2.5,
                "high_atr": 3.0
            },
            "atr_low_threshold": 15.0,
            "atr_high_threshold": 30.0
        }
    }
    
    # Case A: Low ATR (< 15.0) -> entry_z should be 2.0
    bar_low = {
        "near_close": 41000.0,
        "far_close": 41100.0,
        "spread_z": -2.1,
        "atr": 10.0,
        "timestamp": datetime.now()
    }
    market = MarketData(last_bar=bar_low)
    context = StrategyContext(market=market, position=PositionView(size=0), config=config)
    strategy.init(context)
    signal = strategy.on_bar(context)
    # entry_z is 2.0, spread_z is -2.1 -> triggered!
    assert signal is not None
    assert signal.action == "BUY_NEAR_SELL_FAR"

    # Case B: High ATR (> 30.0) -> entry_z should be 3.0
    strategy._reset()
    bar_high = {
        "near_close": 41000.0,
        "far_close": 41100.0,
        "spread_z": -2.8, # Less than 3.0
        "atr": 35.0,
        "timestamp": datetime.now()
    }
    market = MarketData(last_bar=bar_high)
    context = StrategyContext(market=market, position=PositionView(size=0), config=config)
    strategy.init(context)
    signal = strategy.on_bar(context)
    # entry_z is 3.0, spread_z is -2.8 -> not triggered!
    assert signal is None

def test_tick_confirmation_release(strategy):
    # Setup confirm settings
    config = {
        "params": {
            "min_atr": 10.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 3.5,
            "release_stop_points": 20,
            "trail_distance_points": 30,
            "confirm_ticks": 3,
            "confirm_ms": 200.0
        }
    }
    bar = {
        "near_close": 41000.0,
        "far_close": 41100.0,
        "spread_z": -3.0,
        "atr": 15.0, # stop = 30
        "timestamp": datetime.now()
    }
    market = MarketData(last_bar=bar)
    context = StrategyContext(market=market, position=PositionView(size=0), config=config)
    strategy.init(context)
    
    # Enter position (pass entry_ts to bypass 5s release grace period)
    strategy.sync_position("mts-test-123", "LONG", 41000.0, 41100.0, entry_ts=datetime.now() - timedelta(seconds=10))
    assert strategy._has_position is True

    # Near entry is 41000.0 (LONG), Far entry is 41100.0 (SHORT).
    # Near stops at 41000.0 - 30.0 = 40970.0.
    
    # Start the patch for time.monotonic
    with patch("time.monotonic") as mock_mono:
        # Tick 1: set time to 100.0 (Triggered stop condition but not confirmed)
        mock_mono.return_value = 100.0
        bar1 = dict(bar, near_close=40960.0)
        import dataclasses
        context = dataclasses.replace(context, market=dataclasses.replace(context.market, last_bar=bar1))
        signal = strategy.on_bar(context)
        assert signal is None
        assert strategy._release_near_ticks == 1

        # Tick 2: set time to 100.1 (elapsed 100ms, not enough)
        mock_mono.return_value = 100.1
        signal = strategy.on_bar(context)
        assert signal is None
        assert strategy._release_near_ticks == 2

        # Tick 3: set time to 100.3 (elapsed 300ms, ticks=3 >= 3, ms=300 >= 200) -> confirmed!
        mock_mono.return_value = 100.3
        signal = strategy.on_bar(context)
        assert signal is not None
        assert signal.action == "PARTIAL_EXIT"
        assert strategy._lifecycle == "RELEASE_NEAR"

def test_quote_freshness_and_spread_width_gates(strategy):
    config = {
        "params": {
            "min_atr": 10.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 3.5,
            "release_stop_points": 20,
            "trail_distance_points": 30,
            "max_quote_age_ms": 1000.0,
            "max_spread_width": 3.0
        }
    }
    bar = {
        "near_close": 41000.0,
        "far_close": 41100.0,
        "spread_z": -3.0,
        "atr": 15.0,
        "timestamp": datetime.now(),
        "near_tick_age_ms": 1200.0, # Stale!
        "far_tick_age_ms": 100.0,
    }
    market = MarketData(last_bar=bar)
    context = StrategyContext(market=market, position=PositionView(size=0), config=config)
    strategy.init(context)
    
    # Enter position
    strategy.sync_position("mts-test-123", "LONG", 41000.0, 41100.0)
    
    # Attempt release with stale quote age
    bar1 = dict(bar, near_close=40960.0) # stops near
    import dataclasses
    context = dataclasses.replace(context, market=dataclasses.replace(context.market, last_bar=bar1))
    signal = strategy.on_bar(context)
    assert signal is None # blocked by quote age

    # Now make quotes fresh, but spread width wide
    bar2 = dict(bar1, near_tick_age_ms=100.0, near_bid=40955.0, near_ask=40965.0) # width = 10 > 3
    context = dataclasses.replace(context, market=dataclasses.replace(context.market, last_bar=bar2))
    signal = strategy.on_bar(context)
    assert signal is None # blocked by spread width

def test_mfe_trail_tightening(strategy):
    config = {
        "params": {
            "min_atr": 10.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 3.5,
            "release_stop_points": 20,
            "trail_distance_points": 30,
            "mfe_tighten": {
                "enabled": True,
                "level_1_atr": 2.0,
                "level_1_trail_mult": 1.6,
                "level_2_atr": 3.0,
                "level_2_trail_mult": 1.2
            }
        }
    }
    bar = {
        "near_close": 41000.0,
        "far_close": 41100.0,
        "spread_z": -3.0,
        "atr": 10.0,
        "timestamp": datetime.now()
    }
    market = MarketData(last_bar=bar)
    context = StrategyContext(market=market, position=PositionView(size=0), config=config)
    strategy.init(context)
    
    strategy.sync_position("mts-test-123", "LONG", 41000.0, 41100.0)
    
    # Case A: Low MFE -> trail_dist is normal 3.5 * 10 = 35.0
    _, trail_dist = strategy._get_thresholds(bar)
    assert trail_dist == 35.0

    # Case B: MFE >= 2.0 * ATR (20.0) -> trail_dist is tightened to 1.6 * 10 = 16.0 -> floored to 20.0
    strategy._mfe_pts = 25.0
    _, trail_dist = strategy._get_thresholds(bar)
    assert trail_dist == 20.0 # floored to 20

    # Case C: MFE >= 3.0 * ATR (30.0) with larger ATR (e.g. 20.0) to avoid floors
    bar_high_atr = dict(bar, atr=20.0)
    strategy._mfe_pts = 65.0 # > 3.0 * 20.0 (60.0)
    _, trail_dist = strategy._get_thresholds(bar_high_atr)
    # level 2 trail mult: 1.2 * 20.0 = 24.0
    assert trail_dist == 24.0

def test_post_release_breakeven_and_lock(strategy):
    config = {
        "params": {
            "min_atr": 10.0,
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 3.5,
            "release_stop_points": 20,
            "trail_distance_points": 30,
            "confirm_ticks": 1,
            "confirm_ms": 0.0,
            "post_release": {
                "breakeven_after_atr": 1.0,
                "force_lock_after_atr": 3.0
            }
        }
    }
    bar = {
        "near_close": 41000.0,
        "far_close": 41100.0,
        "spread_z": -3.0,
        "atr": 10.0,
        "timestamp": datetime.now()
    }
    market = MarketData(last_bar=bar)
    context = StrategyContext(market=market, position=PositionView(size=0), config=config)
    strategy.init(context)
    
    # Enter position (pass entry_ts to bypass 5s release grace period)
    strategy.sync_position("mts-test-123", "LONG", 41000.0, 41100.0, entry_ts=datetime.now() - timedelta(seconds=10))
    
    # Release near leg (LONG) and transition far leg (SHORT) to trailing
    strategy.sync_release(leg="near", price=41100.0) # far is remaining leg, side is SHORT, entry was 41100.0
    assert strategy._side == "SHORT"
    assert strategy._lifecycle == "TRAILING_SHORT"
    assert strategy._nadir == 41100.0

    # Check Stage 1: Breakeven Stop-loss Adjustment (SHORT entry is 41100.0)
    # If far_close drops to 40990.0 (floating profit = 110.0 > 1.0 * ATR (10.0)),
    # breakeven is activated. Stop price (normally nadir + trail = 40990.0 + 35.0 = 41025.0)
    # should be locked to not exceed entry price 41100.0 (for SHORT).
    # Since 41025.0 <= 41100.0, let's simulate price rebound to 41050.0.
    # If nadir was 40990.0, trail stop is 41025.0.
    # What if price rebound to 41110.0? Without breakeven, stop would be 41110.0 (if nadir was 41080.0, stop 41115.0).
    # Let's verify that stop is adjusted correctly.
    # If remaining leg is SHORT, floating profit = entry - price = 41100.0 - 40990.0 = 110.0.
    # Since 110.0 >= 10.0 (1.0 * ATR), breakeven is active.
    # The stop price _trail_stop = min(_trail_stop, _rem_entry) = min(41025.0, 41100.0) = 41025.0.
    # If the price bounced to 41105.0: _rem_high = 41105.0. This is >= _trail_stop (41025.0), triggering exit!
    # If breakeven was NOT active (e.g. no profit yet, nadir was 41090.0, stop = 41125.0), price 41105.0 would NOT exit because 41105.0 < 41125.0.
    # Let's verify:
    bar1 = dict(bar, far_close=40990.0, far_high=41105.0, far_low=40990.0)
    import dataclasses
    context = dataclasses.replace(context, market=dataclasses.replace(context.market, last_bar=bar1))
    signal = strategy.on_bar(context)
    assert signal is not None
    assert signal.action == "EXIT" # Exited because trail stop was adjusted by breakeven!

    # Reset and Check Stage 3: Force Lock (SHORT entry is 41100.0)
    # If price drops to 40790.0 (floating profit = 310.0 >= 3.0 * ATR (30.0)),
    # it should trigger force lock immediate exit on the very same tick!
    strategy._reset(exit_ts=datetime.now() - timedelta(seconds=600))
    strategy.sync_position("mts-test-123", "LONG", 41000.0, 41100.0, entry_ts=datetime.now() - timedelta(seconds=10))
    strategy.sync_release(leg="near", price=41100.0)
    
    bar2 = dict(bar, far_close=40790.0, far_high=40790.0, far_low=40790.0)
    context = dataclasses.replace(context, market=dataclasses.replace(context.market, last_bar=bar2))
    signal = strategy.on_bar(context)
    assert signal is not None
    assert signal.action == "EXIT"
    assert "FORCE_LOCK" in signal.reason

def test_realized_pnl_drift_fix(strategy):
    # Setup
    strategy.sync_position("mts-test-123", "LONG", 41000.0, 41100.0)
    
    # Confirm release of near leg at price 40900.0
    # Remaining leg (far) price is 41200.0
    strategy.sync_release(leg="near", price=41200.0, release_price=40900.0)
    assert strategy._release_price == 40900.0

    # Verify that in subsequent write_state calls, realized pnl of near leg remains locked to 40900.0
    # and does not float with near_last.
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state") as mock_write:
        strategy.write_state(
            action="TRAILING_LONG",
            reason="test_heartbeat",
            near_last=41500.0, # near floats to 41500.0
            far_last=41250.0,
            spread_z=1.0,
            release_stop_points=20,
            trail_distance_points=30
        )
        mock_write.assert_called_once()
        kwargs = mock_write.call_args.kwargs
        # release price and leg are correctly preserved for internal realized PnL calculations
        assert kwargs.get("release_price") == 40900.0
        assert kwargs.get("released_leg") == "near"

