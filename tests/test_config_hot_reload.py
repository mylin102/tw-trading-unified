
import os
import yaml
import time
import pytest
from datetime import datetime
from strategies.futures.monitor import FuturesMonitor

class MockAPI:
    def __init__(self):
        self.is_logged_in = True

def test_config_hot_reload(tmp_path):
    """測試配置檔案變動時，FuturesMonitor 能自動熱載入新參數"""
    # 1. 準備初始配置檔案
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    config_file = config_dir / "futures_test.yaml"
    
    initial_config = {
        "strategy": {"length": 20, "cooldown_bars": 8},
        "risk_mgmt": {"atr_multiplier": 1.5},
        "execution": {"initial_balance": 100000, "broker_fee_per_side": 20},
        "monitoring": {"poll_interval_secs": 30, "use_order_manager": False},
        "trade_mgmt": {"lots_per_trade": 1}
    }
    
    with open(config_file, "w") as f:
        yaml.dump(initial_config, f)
    
    # 2. 初始化 Monitor
    monitor = FuturesMonitor(api=MockAPI(), config_path=str(config_file), dry_run=True)
    assert monitor.ATR_MULT == 1.5
    assert monitor.STRATEGY["length"] == 20
    
    # 3. 修改配置檔案並強制更新 mtime (Linux/Mac 檔案系統解析度問題)
    new_config = initial_config.copy()
    new_config["risk_mgmt"]["atr_multiplier"] = 2.5
    new_config["strategy"]["length"] = 50
    
    # 延遲一下確保 mtime 真的有變
    time.sleep(0.1)
    with open(config_file, "w") as f:
        yaml.dump(new_config, f)
    
    # 4. 呼叫熱載入檢查
    monitor._reload_config_if_changed()
    
    # 5. 驗證屬性已更新
    assert monitor.ATR_MULT == 2.5
    assert monitor.STRATEGY["length"] == 50
    print("\n✓ Hot-reload verified: ATR_MULT updated to 2.5, length to 50")

def test_trader_params_reload(tmp_path):
    """測試熱載入時，Trader 的參數（手續費等）也會跟著更新"""
    config_file = tmp_path / "futures_trader_test.yaml"
    
    config = {
        "strategy": {}, "risk_mgmt": {},
        "execution": {"initial_balance": 100000, "broker_fee_per_side": 20, "margin_per_lot": 40000},
        "monitoring": {}, "trade_mgmt": {}
    }
    
    with open(config_file, "w") as f:
        yaml.dump(config, f)
        
    monitor = FuturesMonitor(api=MockAPI(), config_path=str(config_file), dry_run=True)
    assert monitor.trader.fee_per_side == 20
    assert monitor.trader.margin_per_lot == 40000
    
    # 更新手續費與保證金
    config["execution"]["broker_fee_per_side"] = 15
    config["execution"]["margin_per_lot"] = 46000
    
    time.sleep(0.1)
    with open(config_file, "w") as f:
        yaml.dump(config, f)
        
    monitor._reload_config_if_changed()
    
    assert monitor.trader.fee_per_side == 15
    assert monitor.trader.margin_per_lot == 46000
    print("✓ Trader params hot-reload verified")


def _make_config(tmp_path):
    """Helper: write minimal futures config and return path."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "futures_test.yaml"
    cfg = {
        "strategy": {"length": 20, "cooldown_bars": 8},
        "risk_mgmt": {"atr_multiplier": 1.5},
        "execution": {"initial_balance": 100000, "broker_fee_per_side": 20},
        "monitoring": {"poll_interval_secs": 30, "use_order_manager": False},
        "trade_mgmt": {"lots_per_trade": 1},
    }
    with open(config_file, "w") as f:
        yaml.dump(cfg, f)
    return config_file


def test_futures_monitor_init_sets_last_tick_at(tmp_path):
    """__init__ must initialize last_tick_at; missing it causes AttributeError on first
    _strategy_tick() call (before any real tick arrives via on_tick)."""
    config_file = _make_config(tmp_path)
    monitor = FuturesMonitor(api=MockAPI(), config_path=str(config_file), dry_run=True)

    assert hasattr(monitor, "last_tick_at"), (
        "last_tick_at must be initialized in __init__ — "
        "it is accessed in _strategy_tick() before any tick arrives"
    )
    # Value must be a recent timestamp, not zero / None
    import time
    assert isinstance(monitor.last_tick_at, float)
    assert monitor.last_tick_at <= time.time()
