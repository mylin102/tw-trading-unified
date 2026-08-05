# 2026-07-31 Antigravity: P0 Tick Routing Near/Far Isolation Test
import time
from pathlib import Path
from unittest.mock import MagicMock
from strategies.futures.monitor import FuturesMonitor

def test_far_month_tick_does_not_pollute_near_month_price():
    # Setup monitor mock
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.last_tick_at = 0
    mon.market_data = {}
    mon.ticker = "TMF"
    mon._last_tmf_price = 0
    mon._debug_feed = False
    mon._debug_tickbar = False
    mon.dry_run = True
    mon.trader = MagicMock(position=0)
    mon.client = MagicMock(_tick_callbacks={})
    mon.cfg = {}
    mon.manual_trade_flag_path = "/tmp/dummy.flag"
    mon._far_current_bar = {"ts": None, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ticks": 0}
    mon._far_last_bar = {}
    mon._last_far_bar_ts = 0
    mon._current_bar = {"ts": None, "open": 0, "high": 0, "low": 0, "close": 0, "volume": 0, "ticks": 0}
    mon._last_bar = {}
    mon._last_bar_ts = 0
    mon.csv_path = Path("/tmp/dummy_ticks.csv")
    mon._trade_mgr = None
    mon._write_raw_tick = MagicMock()
    mon._refresh_runtime_status = MagicMock()
    mon._process_manual_trade_flag = MagicMock()
    mon._mts_tick = MagicMock()
    
    # Near and Far contracts
    mon.contract = MagicMock(code="TMFH6")
    mon.far_contract = MagicMock(code="TMFI6")
    
    # 1. Send Far-month tick (TMFI6 @ 43845)
    far_tick = MagicMock()
    far_tick.code = "TMFI6"
    far_tick.close = 43845.0
    far_tick.buy_price = 43840.0
    far_tick.sell_price = 43850.0
    far_tick.datetime = "2026-07-31 16:21:49"
    
    mon.on_tick("TFE", far_tick)
    
    # Far-month tick MUST be archived but must not pollute near-month state.
    mon._write_raw_tick.assert_called_once_with(far_tick)
    # Far-month tick MUST NOT pollute near-month _last_tmf_price or TMF/TMF_NEAR slots
    assert mon._last_tmf_price == 0, f"Expected 0, got {mon._last_tmf_price}"
    assert "TMF" not in mon.market_data
    assert "TMF_NEAR" not in mon.market_data
    assert "TMF_FAR" in mon.market_data
    assert mon.market_data["TMF_FAR"]["close"] == 43845.0
    assert mon.market_data["TMFI6"]["close"] == 43845.0
    
    # 2. Send Near-month tick (TMFH6 @ 43680)
    near_tick = MagicMock()
    near_tick.code = "TMFH6"
    near_tick.close = 43680.0
    near_tick.buy_price = 43675.0
    near_tick.sell_price = 43685.0
    near_tick.datetime = "2026-07-31 16:21:50"
    
    mon.on_tick("TFE", near_tick)
    
    # Both legs must reach the raw tick writer once.
    assert mon._write_raw_tick.call_count == 2
    # Near-month tick MUST update _last_tmf_price and TMF/TMF_NEAR slots
    assert mon._last_tmf_price == 43680.0
    assert mon.market_data["TMF"]["close"] == 43680.0
    assert mon.market_data["TMF_NEAR"]["close"] == 43680.0
    assert mon.market_data["TMFH6"]["close"] == 43680.0

