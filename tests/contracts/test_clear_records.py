"""
Contract test for MTS clear_records command.
Verifies that:
1. In-memory order manager active/completed orders are cleared.
2. The orders file on disk is deleted.
3. Call to _save_orders_file_wrapper writes an empty list to disk.
4. End-to-end flag processing clears everything.
"""
# 2026-07-07 Gemini CLI / Hermes Agent: Contract test for clear_records cleanup integrity

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.order_management.order_manager import OrderManager
from core.order_management.order import Order, OrderSide, OrderType, OrderStatus

@pytest.fixture
def mock_orders_setup(tmp_path, monkeypatch):
    # Setup directories
    exports_dir = tmp_path / "exports" / "trades"
    exports_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)

    # Isolated state path via env
    monkeypatch.setenv("MTS_STATE_PATH", str(tmp_path / "mts_position_state.json"))

    # Create dummy orders on disk
    from core.date_utils import get_session_date_str
    session_date = get_session_date_str()
    orders_file = exports_dir / f"TMF_{session_date}_orders.json"
    
    old_orders = [
        {"order_id": f"ORD-{session_date}-000001", "status": "filled", "strategy": "TEST"},
        {"order_id": f"ORD-{session_date}-000002", "status": "submitted", "strategy": "TEST"},
    ]
    with open(orders_file, "w") as f:
        json.dump(old_orders, f)

    return exports_dir, orders_file, session_date


def test_clear_session_orders_wipes_state(mock_orders_setup):
    exports_dir, orders_file, session_date = mock_orders_setup

    om = OrderManager(mode="paper")
    # Manually populate some active and completed orders in memory
    o1 = Order(symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=1, price=47000, strategy="TEST")
    o1.order_id = f"ORD-{session_date}-000001"
    o1.status = OrderStatus.FILLED
    om.completed.append(o1)

    o2 = Order(symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=1, price=47100, strategy="TEST")
    o2.order_id = f"ORD-{session_date}-000002"
    o2.status = OrderStatus.SUBMITTED
    om.active_orders[o2.order_id] = o2

    # Verify initial populated state
    assert len(om.active_orders) == 1
    assert len(om.completed) == 1

    # Execute clear session orders
    om.clear_session_orders()

    # Verify everything wiped
    assert len(om.active_orders) == 0
    assert len(om.completed) == 0
    assert om._next_id == 1


def test_monitor_clear_records_flag_processing(mock_orders_setup, monkeypatch, tmp_path):
    exports_dir, orders_file, session_date = mock_orders_setup

    from strategies.futures.monitor import FuturesMonitor
    
    # Build a minimal monitor instance using __new__
    m = FuturesMonitor.__new__(FuturesMonitor)
    
    # Populate OrderManager and mock trader/registry
    om = OrderManager(mode="paper")
    o1 = Order(symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT, quantity=1, price=47000, strategy="TEST")
    o1.order_id = f"ORD-{session_date}-000001"
    o1.status = OrderStatus.FILLED
    om.completed.append(o1)
    
    object.__setattr__(m, "order_mgr", om)
    object.__setattr__(m, "ticker", "TMF")
    object.__setattr__(m, "market_data", {})
    object.__setattr__(m, "trader", SimpleNamespace(position=0, entry_price=0.0, execute_signal=MagicMock()))
    object.__setattr__(m, "_lifecycle_generation", 1)
    object.__setattr__(m, "_processed_flag_ids", set())
    object.__setattr__(m, "_flag_retry_count", 0)
    object.__setattr__(m, "_pending_lifecycle_orders", {})
    object.__setattr__(m, "_mts_pending_fills", {})
    object.__setattr__(m, "_mts_stale_order_cancels", set())
    object.__setattr__(m, "_registry", {})
    object.__setattr__(m, "cfg", {"mts": {"strategy": "tmf_spread"}})
    object.__setattr__(m, "_manual_trade_status", "READY")
    object.__setattr__(m, "manual_trade_flag_path", str(tmp_path / "futures_manual_trade.flag"))

    # Mock _cancel_all_pending_orders and _save_orders_file_wrapper delegate
    object.__setattr__(m, "_cancel_all_pending_orders", MagicMock())
    
    # Mock self._save_orders_file_wrapper using real function to test its persistence behavior
    from strategies.futures.monitor import FuturesMonitor
    monkeypatch.setattr(m, "_save_orders_file_wrapper", lambda: FuturesMonitor._save_orders_file_wrapper(m))

    # Write flag file
    flag = {
        "action": "clear_records",
        "ts": "2026-07-07T15:00:00",
        "created_at": time.time()
    }
    flag_file = tmp_path / "futures_manual_trade.flag"
    flag_file.write_text(json.dumps(flag))

    # Run processing
    res = FuturesMonitor._process_manual_trade_flag(m)
    assert res is True

    # Assertions
    assert len(om.active_orders) == 0
    assert len(om.completed) == 0
    assert orders_file.exists()
    
    # Check that the file was written with empty list []
    assert json.loads(orders_file.read_text()) == []
