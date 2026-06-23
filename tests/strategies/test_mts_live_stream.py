import pytest
import os
import json
from unittest.mock import MagicMock, patch
from datetime import datetime
import pandas as pd
from collections import deque
from strategies.futures.monitor import FuturesMonitor
from core.strategy_context import StrategyContext, MarketData, PositionView
from core.order_management.order import OrderSide, OrderStatus

class ShioajiTickMock:
    def __init__(self, code, dt, close, volume=1):
        self.code = code
        self.datetime = dt
        self.close = close
        self.volume = volume

@pytest.fixture
def monitor(tmp_path, monkeypatch):
    state_path = tmp_path / "mts_position_state.json"
    monkeypatch.setenv("MTS_STATE_PATH", str(state_path))
    config_path = "config/futures_night.yaml"
    m = FuturesMonitor(MagicMock(), config_path, dry_run=False)
    m.ticker = "TMF"
    m.contract = MagicMock(code="TMFF6")
    m.far_contract = MagicMock(code="TMFG6")
    m.cfg["mts"] = {"enabled": True}
    m._use_order_manager = True
    from core.order_management.order_manager import OrderManager
    m.order_mgr = OrderManager(m.api)
    
    # 2026-06-23 Gemini CLI: Unconditionally override the registry with a mock to prevent method attribute errors
    m._registry = MagicMock()

    m._current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}
    m._far_current_bar = {"open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ts": None}
    m._last_bar_ts = 0
    m._tick_bars_deque = deque(maxlen=300)
    m._far_tick_bars_deque = deque(maxlen=300)
    
    m._write_raw_tick = MagicMock()
    m._refresh_runtime_status = MagicMock()
    m._process_manual_trade_flag = MagicMock()
    
    return m, state_path

def test_live_stream_flash_spike(monitor):
    m, state_path = monitor
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    strat = TMFSpread()
    
    config = {
        "ticker": "TMF",
        "params": {
            "min_atr": 5.0, 
            "atr_multiplier_stop": 1.0, 
            "atr_multiplier_trail": 2.0, 
            "release_stop_points": 10, 
            "trail_distance_points": 20
        }
    }
    strat.init(StrategyContext(market=MarketData(last_bar={}, ticker="TMF"), position=PositionView(size=0), config=config))
    m._registry.get.return_value = strat

    # 1. Setup Active Position
    strat._has_position = True
    strat._lifecycle = "TRAILING_LONG"
    strat._side = "LONG"
    strat._released_leg = "near"
    strat._far_entry = 44000.0
    strat._peak = 44000.0
    
    with open(state_path, "w") as f:
        json.dump({"has_position": True, "near_entry": 43900, "far_entry": 44000, "near_side": "SHORT", "far_side": "LONG"}, f)

    dt = datetime(2026, 5, 26, 17, 30, 0)

    # 2. Simulate the Live Tick Stream
    m._spread_loader = MagicMock()
    m._spread_loaded = True
    def mock_enrich(bar_dict):
        bar_dict["atr"] = 10.0
        bar_dict["spread_z"] = 1.0
        if "near_close" not in bar_dict: bar_dict["near_close"] = 43900.0
        if "far_close" not in bar_dict: bar_dict["far_close"] = 44000.0

    m._spread_loader.enrich_bar.side_effect = mock_enrich

    with patch("strategies.futures.monitor.is_taifex_futures_market_open", return_value=True),          patch.object(m.order_mgr, 'submit') as mock_submit:
         
        # Tick 1: Normal tick. Far goes to 44010. Peak -> 44010.
        m.on_tick(None, ShioajiTickMock("TMFF6", dt, 43910))
        m.on_tick(None, ShioajiTickMock("TMFG6", dt, 44010))
        assert strat._peak == 44010.0
        assert mock_submit.call_count == 0 

        # Tick 2: Flash Spike! Far jumps to 44045 intra-bar.
        dt2 = datetime(2026, 5, 26, 17, 35, 0)
        m.on_tick(None, ShioajiTickMock("TMFF6", dt2, 43920))
        m.on_tick(None, ShioajiTickMock("TMFG6", dt2, 44045))
        assert strat._peak == 44045.0 # Peak successfully captured!
        assert mock_submit.call_count == 0

        # Tick 3: Rapid drop. Far drops to 44025.
        # Peak is 44045. Drop is 20 points. Trail distance is 20 (ATR 10 * 2).
        dt3 = datetime(2026, 5, 26, 17, 40, 0)
        m.on_tick(None, ShioajiTickMock("TMFF6", dt3, 43915))
        m.on_tick(None, ShioajiTickMock("TMFG6", dt3, 44025))
        
        # Verify the exit order was submitted
        assert mock_submit.call_count == 1
        order = mock_submit.call_args[0][0]
        assert order.strategy == "MTS_EXIT"
        assert order.side == OrderSide.SELL
        
        # Verify strategy reset itself (simulated after order fill confirmation)
        strat._reset()
        assert strat._has_position is False
        assert strat._peak == 0.0
