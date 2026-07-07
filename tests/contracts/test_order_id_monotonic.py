"""
Verify P1/P2: reindex_orders() recovers counter from disk after PM2 restart,
and create_order() collision guard prevents ID reuse.

Two verification points from the user:
1. After restart with disk orders 000001-000006, next ID = 000007
2. Orders in non-chronological order (emergency/entry/release interleaved)
   — reindex uses max(suffix) + 1, not last entry
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))


@pytest.fixture
def mock_orders_dir(monkeypatch, tmp_path):
    """Create a fake exports/trades/TMF_20260707_orders.json with known orders."""
    exports_dir = tmp_path / "exports" / "trades"
    exports_dir.mkdir(parents=True)

    # Non-chronological order: emergency orders (lower IDs) come AFTER
    # release orders (higher IDs) in the JSON array
    orders_on_disk = [
        {"order_id": "ORD-20260707-000003", "created_at": "2026-07-07T09:23:50", "strategy": "MTS_RELEASE_OCO"},
        {"order_id": "ORD-20260707-000004", "created_at": "2026-07-07T09:23:50", "strategy": "MTS_RELEASE_OCO"},
        {"order_id": "ORD-20260707-000001", "created_at": "2026-07-07T09:25:57", "strategy": "MTS_EMERGENCY"},
        {"order_id": "ORD-20260707-000002", "created_at": "2026-07-07T09:25:57", "strategy": "MTS_EMERGENCY"},
        {"order_id": "ORD-20260707-000005", "created_at": "2026-07-07T09:31:02", "strategy": "MTS_ENTRY"},
        {"order_id": "ORD-20260707-000006", "created_at": "2026-07-07T09:31:02", "strategy": "MTS_ENTRY"},
    ]
    orders_file = exports_dir / "TMF_20260707_orders.json"
    with open(orders_file, "w") as f:
        json.dump(orders_on_disk, f)

    # Patch cwd so the relative path in reindex_orders resolves
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_reindex_from_unsorted_disk(mock_orders_dir):
    """P1: reindex_orders() finds max suffix from unsorted disk orders."""
    from core.order_management.order_manager import OrderManager

    om = OrderManager(mode="paper")
    # After init + auto-reindex, _next_id should be max(1..6) + 1 = 7
    assert om._next_id == 7, f"Expected _next_id=7 after reindex, got {om._next_id}"


def test_next_order_after_restart(mock_orders_dir):
    """P1+P2: next order after restart with disk orders = 000007."""
    from core.order_management.order_manager import OrderManager
    from core.order_management.order import OrderSide, OrderType

    om = OrderManager(mode="paper")

    o = om.create_order("TMF_NEAR", OrderSide.BUY, OrderType.LIMIT, 1, 47000, strategy="TEST")
    assert o.order_id == "ORD-20260707-000007", f"Expected 000007, got {o.order_id}"

    o2 = om.create_order("TMF_FAR", OrderSide.SELL, OrderType.LIMIT, 1, 47200, strategy="TEST")
    assert o2.order_id == "ORD-20260707-000008", f"Expected 000008, got {o2.order_id}"


def test_collision_guard_skips_existing(mock_orders_dir):
    """P2: create_order() collision guard skips IDs already in active_orders or completed."""
    from core.order_management.order_manager import OrderManager
    from core.order_management.order import OrderSide, OrderType, Order, OrderStatus

    om = OrderManager(mode="paper")

    # Manually inject order 000007 into completed (simulating race)
    ghost = Order(
        symbol="TMF_NEAR", side=OrderSide.BUY, order_type=OrderType.LIMIT,
        quantity=1, price=47000, strategy="GHOST",
    )
    ghost.order_id = "ORD-20260707-000007"
    ghost.status = OrderStatus.FILLED
    om.completed.append(ghost)

    # create_order should skip 000007 and use 000008
    o = om.create_order("TMF_NEAR", OrderSide.BUY, OrderType.LIMIT, 1, 47000, strategy="TEST")
    assert o.order_id == "ORD-20260707-000008", f"Collision guard failed: expected 000008, got {o.order_id}"
