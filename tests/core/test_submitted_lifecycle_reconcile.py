import time

from core.order_management.order import (
    Order, OrderSide, OrderStatus, OrderType,
)
from core.order_management.order_manager import OrderManager


def _order(status=OrderStatus.SUBMITTED):
    order = Order(
        symbol="TMFJ6-TMFK6", side=OrderSide.SELL,
        order_type=OrderType.LIMIT, quantity=1, price=-172,
        strategy="MTS_ENTRY", order_id="ORD-20260922-000130",
        combo_legs=[
            {"symbol": "TMFJ6", "side": "sell", "quantity": 1},
            {"symbol": "TMFK6", "side": "buy", "quantity": 1},
        ],
    )
    order.status = status
    order.broker_order_id = "8e91b753"
    order.exchange_order_id = "8e91b753"
    order.ordno = "8e91b753"
    return order


def _snapshot(*, positions=None, open_orders=None, broker_trades=None,
              capture="OK", query_failures=None, errors=None,
              captured_at=None):
    return {
        "source": "live_broker",
        "captured_at": (int(time.time() * 1000)
                        if captured_at is None else captured_at),
        "fetch_status": {"capture": capture},
        "query_failures": list(query_failures or []),
        "errors": dict(errors or {}),
        "positions": list(positions or []),
        "open_orders": list(open_orders or []),
        "broker_trades": list(broker_trades or []),
    }


def _manager(order):
    manager = OrderManager(mode="live")
    manager.active_orders[order.order_id] = order
    return manager


def test_submitted_ioc_broker_flat_converges_received_not_filled():
    manager = _manager(_order())

    changed = manager.reconcile_pending_terminal(_snapshot())

    assert changed == [{
        "order_id": "ORD-20260922-000130",
        "action": "APPLY_ZERO_FILL_TERMINAL",
    }]
    assert manager.active_orders == {}
    assert manager.completed[0].status == OrderStatus.RECEIVED_NOT_FILLED
    assert manager.completed[0].is_completed() is True
    assert manager.completed[0].idempotency_key is None


def test_submitted_reconcile_ignores_execution_context_quarantine():
    manager = _manager(_order())
    manager.execution_context = type("Ctx", (), {
        "effective_mode": "live_quarantined",
        "live_order_allowed": False,
        "audit_reasons": ["SESSION_RECONNECT_PENDING"],
    })()

    manager.reconcile_pending_terminal(_snapshot())

    assert manager.completed[0].status == OrderStatus.RECEIVED_NOT_FILLED


def test_submitted_invalid_or_unavailable_snapshot_is_retained():
    bad_snapshots = [
        _snapshot(capture="FAIL"),
        _snapshot(query_failures=["OPEN_ORDERS_UNAVAILABLE"]),
        _snapshot(errors={"positions": "SessionNotEstablished"}),
        _snapshot(captured_at=int(time.time() * 1000) - 60_001),
        None,
    ]
    for snapshot in bad_snapshots:
        manager = _manager(_order())
        assert manager.reconcile_pending_terminal(snapshot) == []
        assert manager.active_orders["ORD-20260922-000130"].status == OrderStatus.SUBMITTED
        assert manager.completed == []


def test_submitted_working_order_retained_and_matching_fill_applied():
    working = _manager(_order())
    assert working.reconcile_pending_terminal(_snapshot(open_orders=[{
        "broker_order_id": "8e91b753", "status": "Submitted",
    }])) == []
    assert working.active_orders["ORD-20260922-000130"].status == OrderStatus.SUBMITTED

    filled = _manager(_order())
    filled.reconcile_pending_terminal(_snapshot(broker_trades=[{
        "broker_order_id": "8e91b753", "status": "Filled",
        "filled_quantity": 1,
    }]))
    assert filled.completed[0].status == OrderStatus.FILLED


def test_partial_filled_is_always_retained():
    manager = _manager(_order(OrderStatus.PARTIAL_FILLED))

    assert manager.reconcile_pending_terminal(_snapshot()) == []
    assert manager.active_orders["ORD-20260922-000130"].status == OrderStatus.PARTIAL_FILLED


def test_maintenance_entry_lock_blocks_entry_but_not_reconciliation(monkeypatch):
    from strategies.plugins.futures.active import tmf_spread

    monkeypatch.setattr(tmf_spread, "_maintenance_entry_lock_active",
                        lambda: True)
    assert tmf_spread._maintenance_entry_lock_active() is True

    manager = _manager(_order())
    manager.reconcile_pending_terminal(_snapshot())

    assert manager.completed[0].status == OrderStatus.RECEIVED_NOT_FILLED
