import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta
from pathlib import Path
import yaml
import os
from strategies.stocks.monitor import StockMonitor

@pytest.fixture
def mock_api():
    api = MagicMock()
    # Mock some basic shioaji attributes if needed
    return api

@pytest.fixture
def config_file(tmp_path):
    config = {
        "stocks": {
            "watchlist": ["2330"],
            "strategy": "mean_reversion",
            "bear_defense": {
                "enabled": True,
                "max_daily_loss": 3000,
                "max_consecutive_losses": 3
            }
        },
        "live_trading": False
    }
    cfg_path = tmp_path / "stocks.yaml"
    with open(cfg_path, "w") as f:
        yaml.dump(config, f)
    return cfg_path

def test_stock_monitor_date_rollover_resets_risk_state_and_paths(mock_api, config_file, tmp_path):
    # Setup paths to use tmp_path
    with patch("strategies.stocks.monitor.TRADE_LOGS", tmp_path):
        monitor = StockMonitor(mock_api, str(config_file))
        
        # Initial state
        initial_date = datetime.now().strftime("%Y%m%d")
        monitor.date_str = initial_date
        monitor.daily_pnl = -1000.0
        monitor.consecutive_losses = 2
        
        initial_ledger = monitor.ledger_path
        initial_orders = monitor.orders_path
        
        # Simulate date change
        future_date = (datetime.now() + timedelta(days=1)).strftime("%Y%m%d")
        
        with patch("strategies.stocks.monitor.datetime") as mock_datetime:
            # Mock datetime.now() to return a future date
            mock_now = datetime.now() + timedelta(days=1)
            mock_datetime.now.return_value = mock_now
            mock_datetime.strftime = datetime.strftime # keep strftime working
            
            # Trigger reset check
            monitor._check_date_reset()
            
            # Verify reset
            assert monitor.date_str == future_date
            assert monitor.daily_pnl == 0.0
            assert monitor.consecutive_losses == 0
            
            # Verify paths updated
            assert monitor.ledger_path != initial_ledger
            assert future_date in str(monitor.ledger_path)
            assert monitor.orders_path != initial_orders
            assert future_date in str(monitor.orders_path)
            
            # Verify scan results cleared
            assert monitor.scan_results == {}
