# -*- coding: utf-8 -*-
"""
Unit tests for the Adaptive Trade Dataset Pipeline.
Author: Gemini CLI
Date: 2026-07-01
"""

import sys
import os
import json
import csv
from pathlib import Path
import pytest
import pandas as pd
from unittest.mock import patch, mock_open

# Import functions from the script
from scripts.generate_adaptive_dataset import (
    parse_mts_trades,
    parse_standard_trades,
    parse_blocked_signals,
    parse_options_trades,
    build_dataset
)

@pytest.fixture
def mock_mts_files(tmp_path):
    """
    Fixture to create temporary mock files for MTS trades.
    """
    fills_file = tmp_path / "mts_trade_fills.jsonl"
    events_file = tmp_path / "mts_spread_events.jsonl"
    
    fill_data = [
        {"timestamp": "2026-06-30T15:14:22.259258", "ticker": "TMF", "contract": "NEAR", "leg": "NEAR", "side": "LONG", "qty": 1, "price": 46610.0, "fill_type": "ENTRY", "trade_id": "mts-auto-151421-850", "session": "night", "spread_z": 3.0, "realized_pnl": None},
        {"timestamp": "2026-06-30T15:14:22.283507", "ticker": "TMF", "contract": "FAR", "leg": "FAR", "side": "SHORT", "qty": 1, "price": 46812.0, "fill_type": "ENTRY", "trade_id": "mts-auto-151421-850", "session": "night", "spread_z": 3.0, "realized_pnl": None},
        {"timestamp": "2026-06-30T15:34:25.216908", "ticker": "TMF", "contract": "FAR", "leg": "FAR", "side": "BUY", "qty": 1, "price": 46907.0, "fill_type": "RELEASE", "trade_id": "mts-auto-151421-850", "session": "night", "spread_z": None, "realized_pnl": -988.7},
        {"timestamp": "2026-06-30T15:55:16.648056", "ticker": "TMF", "contract": "NEAR", "leg": "NEAR", "side": "SELL", "qty": 1, "price": 46580.0, "fill_type": "EXIT", "trade_id": "mts-auto-151421-850", "session": "night", "spread_z": None, "realized_pnl": -338.6}
    ]
    
    event_data = [
        {"event": "ENTRY", "ts": "2026-06-30T15:14:22.292964", "action": "BUY_NEAR_SELL_FAR", "near_side": "LONG", "far_side": "SHORT", "near_entry": 46610.0, "far_entry": 46812.0, "spread_z": 3.0, "trade_id": "mts-auto-151421-850"},
        {"event": "RELEASE_FAR_SUBMITTED", "ts": "2026-06-30T15:34:25.037496", "released_leg": "FAR", "remaining_leg": "NEAR", "trade_id": "mts-auto-151421-850", "atr": 92.93}
    ]
    
    with open(fills_file, "w") as f:
        for item in fill_data:
            f.write(json.dumps(item) + "\n")
            
    with open(events_file, "w") as f:
        for item in event_data:
            f.write(json.dumps(item) + "\n")
            
    return fills_file, events_file

def test_parse_mts_trades(mock_mts_files):
    """
    Test parsing of MTS trades.
    """
    fills_file, events_file = mock_mts_files
    
    with patch("scripts.generate_adaptive_dataset.Path") as mock_path:
        # Mock Path behaviour to redirect to mock files
        def side_effect(arg):
            if "mts_trade_fills" in str(arg):
                return fills_file
            if "mts_spread_events" in str(arg):
                return events_file
            return Path(arg)
        mock_path.side_effect = side_effect
        
        trades = parse_mts_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade["trade_id"] == "mts-auto-151421-850" or "mts-auto-151421-850" in trade["supporting_context"]["legs"]["NEAR"]["trade_id"]
        # Float math check
        assert abs(trade["realized_pnl"] - (-1327.3)) < 0.01
        assert trade["atr"] == 92.93
        assert trade["strategy_name"] == "tmf_spread"

def test_parse_standard_trades(tmp_path):
    """
    Test parsing of standard directional trades.
    """
    attr_file = tmp_path / "trade_attribution.csv"
    
    # Create mock attribution data
    data = [
        {
            "timestamp": "2026-05-21T15:51:14",
            "trade_id": "FUT-20260521155114",
            "strategy": "adaptive_orb_v15",
            "regime": "NORMAL",
            "features": json.dumps({
                "momentum": -146.1,
                "mom_velo": 25.4,
                "vwap_distance_pts": 558.5,
                "atr": 61.9,
                "regime": "NORMAL",
                "score": -33.3,
                "entry_price": 41900.0,
                "signal": "SELL"
            }),
            "outcome": json.dumps({
                "pnl": -4786.6,
                "pnl_pts": -473.0,
                "exit_price": 41427.0,
                "exit_reason": "STOP_LOSS",
                "friction_cost": 40.0
            }),
            "attribution": "{}"
        }
    ]
    
    with open(attr_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "trade_id", "strategy", "regime", "features", "outcome", "attribution"])
        writer.writeheader()
        writer.writerows(data)
        
    with patch("scripts.generate_adaptive_dataset.Path") as mock_path:
        mock_path.return_value = attr_file
        trades = parse_standard_trades()
        
        assert len(trades) == 1
        trade = trades[0]
        assert trade["strategy_name"] == "adaptive_orb_v15"
        assert trade["realized_pnl"] == -4786.6
        assert trade["pnl_pts"] == -473.0
        assert trade["atr"] == 61.9
        assert trade["score"] == -33.3
        assert trade["signal_side"] == "SELL"

def test_parse_blocked_signals(tmp_path):
    """
    Test parsing and merging of blocked/no-entry signals.
    """
    audit_file = tmp_path / "TMF_20260630_signals_audit.csv"
    indicators_file = tmp_path / "TMF_20260630_PAPER_indicators.csv"
    
    audit_data = [
        {
            "timestamp": "2026-06-30 15:10:00",
            "signal": "SQUEEZE_SCOUT",
            "price": "46610.0",
            "reason": "ATR_TOO_LOW",
            "rejection": "ATR 8.5 < min_atr 10.0",
            "lots": "0"
        }
    ]
    
    indicator_data = [
        {
            "timestamp": "2026-06-30 15:10:00",
            "score": 15.0,
            "momentum": 42.0,
            "price_vs_vwap": 25.0,
            "atr": 8.5,
            "regime": "LOW_VOL",
            "session": 2
        }
    ]
    
    # Save files
    with open(audit_file, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["timestamp", "signal", "price", "reason", "rejection", "lots"])
        writer.writeheader()
        writer.writerows(audit_data)
        
    pd.DataFrame(indicator_data).to_csv(indicators_file, index=False)
    
    with patch("scripts.generate_adaptive_dataset.glob.glob") as mock_glob:
        mock_glob.return_value = [str(audit_file)]
        
        blocked = parse_blocked_signals()
        
        assert len(blocked) == 1
        row = blocked[0]
        assert row["strategy_name"] == "SQUEEZE_SCOUT"
        assert row["execution_status"] == "blocked"
        assert row["atr"] == 8.5
        assert row["score"] == 15.0
        assert row["regime"] == "LOW_VOL"
        assert row["session"] == "night"
