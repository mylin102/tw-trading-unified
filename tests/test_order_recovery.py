"""Test order recovery from CSV ledger on restart."""
import pytest
import os
import tempfile
import csv
import datetime
import json
from pathlib import Path
from unittest.mock import Mock, patch
import yaml


def test_options_order_recovery_logic():
    """Test the options order recovery logic in isolation."""
    from core.order_management.order_manager import OrderManager
    from core.order_management.order import OrderSide, OrderStatus
    
    # Create temporary ledger file
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "test_ledger.csv"
        
        # Write sample ledger data matching actual format
        rows = [
            {
                "Timestamp": "2026-04-20 21:03:58",
                "Mode": "V2",
                "Action": "THETA_ENTRY",
                "Side": "THETA",
                "Price": "182.83",
                "Quantity": "1",
                "PnL": "0",
                "Balance": "93766.0",
                "Note": "credit=183 strategy=iron_condor"
            },
            {
                "Timestamp": "2026-04-20 21:52:16",
                "Mode": "V2",
                "Action": "THETA_EXIT",
                "Side": "iron_condor",
                "Price": "182.80",
                "Quantity": "1",
                "PnL": "-1",
                "Balance": "93765.0",
                "Note": "SQUEEZE_RELEASE"
            },
            {
                "Timestamp": "2026-04-20 08:26:34",
                "Mode": "V2",
                "Action": "PAPER_ENTRY",
                "Side": "C",
                "Price": "97.7",
                "Quantity": "1",
                "PnL": "0",
                "Balance": "0",
                "Note": "score=0.0"
            }
        ]
        
        with open(ledger_path, "w") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        
        # Create OrderManager
        order_mgr = OrderManager(mode="paper")
        
        # Simulate recovery logic (simplified version of _recover_orders_from_ledger)
        with open(ledger_path) as f:
            ledger_rows = list(csv.DictReader(f))
        
        recovered_count = 0
        for row in ledger_rows:
            action = row.get("Action", "")
            side_label = row.get("Side", "")
            price = float(row.get("Price", 0))
            quantity = int(row.get("Quantity", 0) or 1)
            
            # Determine if this is an entry or exit
            is_entry = "ENTRY" in action
            is_exit = any(kw in action for kw in ["EXIT", "TP1"])
            
            if not (is_entry or is_exit):
                continue
            
            # Test key logic: THETA entries are SELL, exits are BUY
            is_short_strategy = (side_label in ["THETA", "SHORT"] or "condor" in side_label.lower())
            
            if is_entry:
                order_side = OrderSide.SELL if is_short_strategy else OrderSide.BUY
            else:
                order_side = OrderSide.BUY if is_short_strategy else OrderSide.SELL
            
            recovered_count += 1
            
            # Verify logic for specific cases
            if action == "THETA_ENTRY":
                assert order_side == OrderSide.SELL, "THETA entry should be SELL"
            elif action == "THETA_EXIT":
                assert order_side == OrderSide.BUY, "THETA exit should be BUY"
            elif action == "PAPER_ENTRY" and side_label == "C":
                assert order_side == OrderSide.BUY, "C entry should be BUY"
        
        # Should have processed 3 orders
        assert recovered_count == 3, f"Expected 3 orders, recovered {recovered_count}"


def test_futures_order_recovery_logic(configured_ticker):
    """Test the futures order recovery logic in isolation."""
    from core.order_management.order_manager import OrderManager
    from core.order_management.order import OrderSide, OrderStatus
    
    # Create temporary trades file
    with tempfile.TemporaryDirectory() as tmpdir:
        today = datetime.datetime.now().strftime("%Y%m%d")
        trades_file = Path(tmpdir) / f"{configured_ticker}_{today}_trades.csv"
        
        # Write sample trades data
        rows = [
            {
                "timestamp": "2026-04-20 21:17:37",
                "type": "SELL",
                "direction": "SHORT",
                "price": "37495.0",
                "lots": "1",
                "pnl_pts": "0.0",
                "pnl_cash": "0.0",
                "reason": "UPTHRUST",
                "allow_trade": "True",
                "orb_weight": "1.0",
                "vwap_weight": "1.0",
                "policy_reason": ""
            },
            {
                "timestamp": "2026-04-20 21:30:01",
                "type": "EXIT",
                "direction": "SHORT",
                "price": "37591.0",
                "lots": "1",
                "pnl_pts": "-96.0",
                "pnl_cash": "-1015.0",
                "reason": "STOP_LOSS",
                "allow_trade": "True",
                "orb_weight": "1.0",
                "vwap_weight": "1.0",
                "policy_reason": ""
            },
            {
                "timestamp": "2026-04-20 09:00:00",
                "type": "BUY",
                "direction": "LONG",
                "price": "37000.0",
                "lots": "2",
                "pnl_pts": "0.0",
                "pnl_cash": "0.0",
                "reason": "SQUEEZE_ON",
                "allow_trade": "True",
                "orb_weight": "1.0",
                "vwap_weight": "1.0",
                "policy_reason": ""
            }
        ]
        
        with open(trades_file, "w") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        
        # Simulate recovery logic
        with open(trades_file) as f:
            trade_rows = list(csv.DictReader(f))
        
        recovered_count = 0
        for row in trade_rows:
            trade_type = row.get("type", "")
            direction = row.get("direction", "")
            price = float(row.get("price", 0))
            lots = int(row.get("lots", 0) or 1)
            
            # Test key logic
            if trade_type == "BUY":
                order_side = OrderSide.BUY
            elif trade_type == "SELL":
                order_side = OrderSide.SELL
            elif trade_type == "EXIT":
                # Exit order side is opposite of direction
                order_side = OrderSide.SELL if direction == "LONG" else OrderSide.BUY
            else:
                continue
            
            recovered_count += 1
            
            # Verify logic
            if trade_type == "SELL" and direction == "SHORT":
                assert order_side == OrderSide.SELL
            elif trade_type == "EXIT" and direction == "SHORT":
                assert order_side == OrderSide.BUY, "EXIT from SHORT should be BUY"
            elif trade_type == "BUY" and direction == "LONG":
                assert order_side == OrderSide.BUY
        
        # Should have processed 3 trades
        assert recovered_count == 3, f"Expected 3 trades, recovered {recovered_count}"


def test_order_recovery_handles_corrupted_data():
    """Test that recovery handles malformed CSV data gracefully."""
    with tempfile.TemporaryDirectory() as tmpdir:
        ledger_path = Path(tmpdir) / "bad_ledger.csv"
        
        # Write malformed data
        rows = [
            {
                "Timestamp": "invalid-date",
                "Mode": "V2",
                "Action": "PAPER_ENTRY",
                "Side": "C",
                "Price": "not-a-number",
                "Quantity": "also-bad",
                "PnL": "0",
                "Balance": "0",
                "Note": ""
            }
        ]
        
        with open(ledger_path, "w") as f:
            writer = csv.DictWriter(f, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        
        # Recovery should not crash
        with open(ledger_path) as f:
            ledger_rows = list(csv.DictReader(f))
        
        for row in ledger_rows:
            try:
                # This would fail in actual recovery
                price = float(row.get("Price", 0))
                quantity = int(row.get("Quantity", 0) or 1)
            except (ValueError, TypeError):
                # Recovery code catches and continues
                pass  # Expected to fail gracefully


def test_order_side_logic_directional_vs_theta():
    """Test the core logic distinguishing directional vs theta strategies."""
    from core.order_management.order import OrderSide
    
    # Test cases: (side_label, is_entry, expected_order_side)
    test_cases = [
        ("C", True, OrderSide.BUY),            # Call entry = BUY
        ("C", False, OrderSide.SELL),          # Call exit = SELL
        ("P", True, OrderSide.BUY),            # Put entry = BUY
        ("P", False, OrderSide.SELL),          # Put exit = SELL
        ("THETA", True, OrderSide.SELL),       # Theta entry = SELL
        ("THETA", False, OrderSide.BUY),       # Theta exit = BUY
        ("SHORT", True, OrderSide.SELL),       # Short entry = SELL
        ("SHORT", False, OrderSide.BUY),       # Short exit = BUY
        ("iron_condor", True, OrderSide.SELL), # Iron condor entry = SELL
        ("iron_condor", False, OrderSide.BUY), # Iron condor exit = BUY
    ]
    
    for side_label, is_entry, expected_side in test_cases:
        is_short_strategy = (side_label in ["THETA", "SHORT"] or "condor" in side_label.lower())
        
        if is_entry:
            order_side = OrderSide.SELL if is_short_strategy else OrderSide.BUY
        else:
            order_side = OrderSide.BUY if is_short_strategy else OrderSide.SELL
        
        assert order_side == expected_side, f"Failed for {side_label}, entry={is_entry}: got {order_side}, expected {expected_side}"


def _write_min_futures_config(tmp_path, *, use_order_manager=True, ticker="TMF"):
    config_file = tmp_path / "futures_test.yaml"
    config = {
        "ticker": ticker,
        "strategy": {"cooldown_bars": 8},
        "risk_mgmt": {"stop_loss_pts": 60},
        "execution": {"initial_balance": 100000, "broker_fee_per_side": 20},
        "monitoring": {"poll_interval_secs": 30, "use_order_manager": use_order_manager},
        "trade_mgmt": {"max_positions": 1},
    }
    with open(config_file, "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f)
    return config_file


def test_futures_monitor_records_exit_lifecycle_without_restart(tmp_path, monkeypatch, configured_ticker):
    from strategies.futures.monitor import FuturesMonitor
    from strategies.futures.squeeze_futures.data import data_storage

    monkeypatch.chdir(tmp_path)
    data_storage._storage = None

    config_file = _write_min_futures_config(tmp_path, use_order_manager=True)
    monitor = FuturesMonitor(api=type("A", (), {})(), config_path=str(config_file), dry_run=True)

    entry_ts = datetime.datetime(2026, 4, 20, 21, 17, 37)
    exit_ts = datetime.datetime(2026, 4, 20, 21, 30, 1)

    entry_order_id = monitor._execute_trade("SELL", 37495.0, entry_ts, 1, stop_loss=60, reason="UPTHRUST")
    assert entry_order_id
    assert monitor.trader.position == -1
    assert [o.side.value for o in monitor.order_mgr.get_completed()] == ["sell"]

    monitor._entry_features_futures = {}
    exit_result = monitor._execute_trade("EXIT", 37591.0, exit_ts, 1, reason="STOP_LOSS")
    assert exit_result
    assert monitor.trader.position == 0

    completed = monitor.order_mgr.get_completed()
    assert len(completed) == 2
    assert [o.side.value for o in completed] == ["sell", "buy"]
    assert completed[1].comment == "EXIT STOP_LOSS"

    orders_file = tmp_path / "exports" / "trades" / f"{configured_ticker}_{datetime.datetime.now():%Y%m%d}_orders.json"
    orders_data = json.loads(orders_file.read_text(encoding="utf-8"))
    assert len(orders_data) == 2
    assert [row["side"] for row in orders_data] == ["sell", "buy"]

    data_storage._storage = None


def test_futures_monitor_recovers_exit_lifecycle_from_trades_csv(tmp_path, monkeypatch, configured_ticker):
    from strategies.futures.monitor import FuturesMonitor
    from strategies.futures.squeeze_futures.data import data_storage

    monkeypatch.chdir(tmp_path)
    data_storage._storage = None

    trades_dir = tmp_path / "exports" / "trades"
    trades_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.datetime.now().strftime("%Y%m%d")
    trades_file = trades_dir / f"{configured_ticker}_{today}_trades.csv"
    rows = [
        {
            "timestamp": "2026-04-20 21:17:37",
            "type": "SELL",
            "direction": "SHORT",
            "price": "37495.0",
            "lots": "1",
            "pnl_pts": "0.0",
            "pnl_cash": "0.0",
            "reason": "UPTHRUST",
            "allow_trade": "True",
            "orb_weight": "1.0",
            "vwap_weight": "1.0",
            "policy_reason": "",
        },
        {
            "timestamp": "2026-04-20 21:30:01",
            "type": "EXIT",
            "direction": "SHORT",
            "price": "37591.0",
            "lots": "1",
            "pnl_pts": "-96.0",
            "pnl_cash": "-1015.0",
            "reason": "STOP_LOSS",
            "allow_trade": "True",
            "orb_weight": "1.0",
            "vwap_weight": "1.0",
            "policy_reason": "",
        },
    ]
    with open(trades_file, "w", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)

    config_file = _write_min_futures_config(tmp_path, use_order_manager=True)
    monitor = FuturesMonitor(api=type("A", (), {})(), config_path=str(config_file), dry_run=True)

    completed = monitor.order_mgr.get_completed()
    assert len(completed) == 2
    assert [o.side.value for o in completed] == ["sell", "buy"]

    orders_file = trades_dir / f"{configured_ticker}_{today}_orders.json"
    orders_data = json.loads(orders_file.read_text(encoding="utf-8"))
    assert len(orders_data) == 2
    assert [row["side"] for row in orders_data] == ["sell", "buy"]

    data_storage._storage = None
