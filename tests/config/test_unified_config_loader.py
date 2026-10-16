import os
import yaml
import pytest
from pathlib import Path
from strategies.futures.monitor import FuturesMonitor

def test_unified_config_base_inheritance(tmp_path):
    cfg_file = tmp_path / "futures.yaml"
    cfg_data = {
        "trade_mgmt": {
            "max_positions": 4,
            "lots_per_trade": 1
        },
        "session_overrides": {
            "night": {
                "risk_mgmt": {"stop_loss_pts": 80}
            }
        }
    }
    cfg_file.write_text(yaml.dump(cfg_data))
    
    mon = FuturesMonitor(api=None, config_path=str(cfg_file), dry_run=True)
    assert mon.cfg["trade_mgmt"]["max_positions"] == 4
    assert mon.cfg["trade_mgmt"]["lots_per_trade"] == 1

def test_unified_config_session_override_merging():
    primary_file = Path("config/futures.yaml")
    original_text = primary_file.read_text()
    
    try:
        cfg_data = yaml.safe_load(original_text)
        cfg_data["trade_mgmt"]["max_positions"] = 4
        cfg_data["session_overrides"] = {
            "night": {
                "risk_mgmt": {
                    "stop_loss_pts": 80
                }
            }
        }
        primary_file.write_text(yaml.dump(cfg_data))
        
        mon = FuturesMonitor(api=None, config_path="config/futures_night.yaml", dry_run=True)
        assert mon.cfg["trade_mgmt"]["max_positions"] == 4
        assert mon.cfg["risk_mgmt"]["stop_loss_pts"] == 80
    finally:
        primary_file.write_text(original_text)
