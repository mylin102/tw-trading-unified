import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime
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
    assert strategy._has_position is True

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
