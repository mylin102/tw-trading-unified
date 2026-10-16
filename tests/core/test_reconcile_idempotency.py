"""Reconcile idempotency: a second pass over the same broker receipts must not
re-process already-FILLED orders (which would regress them via attach_submission
and re-trigger an orders-export save).  Locks:
- reconcile_broker_state is idempotent: same snapshot twice -> second pass empty
- reconcile_position_covered_orders skips already-FILLED orders
- the covered-position happy path still reconciles PENDING/SUBMITTED orders
"""
from types import SimpleNamespace

from core.order_management.order import OrderSide, OrderStatus
from core.order_management.order_manager import OrderManager
from strategies.futures.monitor import FuturesMonitor
from tests.core.test_broker_reconciliation_and_session_close import _FakeApi, _submitted


def _manager_with_orders():
    manager = OrderManager(mode="live")
    near = _submitted(manager, "TMFH6", OrderSide.SELL, "BRK-NEAR")
    far = _submitted(manager, "TMFI6", OrderSide.BUY, "BRK-FAR")
    return manager, near, far


def test_reconcile_broker_state_second_pass_is_idempotent():
    manager, near, far = _manager_with_orders()
    trades = _FakeApi().list_trades()

    r1 = manager.reconcile_broker_state(filled_trades=trades,
                                        source="test", reason="idem")
    r2 = manager.reconcile_broker_state(filled_trades=trades,
                                        source="test", reason="idem")

    assert len(r1["reconciled"]) == 2
    assert r1["reconciled"][0]["fills_added"] == 1
    assert r2["reconciled"] == []          # idempotent — no re-processing
    assert near.status is OrderStatus.FILLED
    assert far.status is OrderStatus.FILLED
    assert near.filled_quantity == 1


def test_reconcile_broker_state_does_not_regress_filled_status():
    manager, near, far = _manager_with_orders()
    trades = _FakeApi().list_trades()
    manager.reconcile_broker_state(filled_trades=trades,
                                   source="test", reason="idem")
    assert near.status is OrderStatus.FILLED

    # A duplicate open-order receipt for the same orders must not regress FILLED.
    dup = trades + trades
    manager.reconcile_broker_state(filled_trades=dup,
                                   source="test", reason="dup")
    assert near.status is OrderStatus.FILLED
    assert far.status is OrderStatus.FILLED
    assert near.filled_quantity == 1
    assert far.filled_quantity == 1


def test_reconcile_position_covered_orders_skips_filled_orders():
    manager, near, far = _manager_with_orders()
    # Both orders already FILLED (prior pass applied broker trades).
    for order in (near, far):
        order.status = OrderStatus.FILLED
        order.filled_quantity = 1
    result = manager.reconcile_position_covered_orders([
        {"account": "futures", "code": "TMFH6", "direction": "Sell",
         "quantity": 1, "avg_cost": 46156},
        {"account": "futures", "code": "TMFI6", "direction": "Buy",
         "quantity": 1, "avg_cost": 46329},
    ], captured_at=1786675115068)
    assert result["reconciled"] == []


def test_covered_position_happy_path_still_reconciles_submitted_orders():
    manager = OrderManager(mode="live")
    near = _submitted(manager, "TMFH6", OrderSide.SELL, "BRK-NEAR")
    far = _submitted(manager, "TMFI6", OrderSide.BUY, "BRK-FAR")
    result = manager.reconcile_position_covered_orders([
        {"account": "futures", "code": "TMFH6", "direction": "Sell",
         "quantity": 1, "avg_cost": 46156},
        {"account": "futures", "code": "TMFI6", "direction": "Buy",
         "quantity": 1, "avg_cost": 46329},
    ], captured_at=1786675115068)
    assert {row["order_id"] for row in result["reconciled"]} == {
        near.order_id, far.order_id}
    assert near.status is OrderStatus.FILLED
    assert far.status is OrderStatus.FILLED
    assert near.avg_fill_price == 46156


def test_refresh_flow_saves_orders_export_once():
    """The monitor refresh runs the reconcile twice (capture boundary + refresh
    boundary) but the orders export must be saved exactly once."""
    manager, near, far = _manager_with_orders()
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor.api = _FakeApi()
    monitor.order_mgr = manager
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._execution_context = SimpleNamespace(
        requested_mode="live", effective_mode="live_ready",
        session_id="session-1")
    monitor._live_broker_authority_at = 0.0
    monitor._live_broker_authority = None
    monitor._persist_current_session_canonical = lambda snapshot: None
    saved = []
    monitor._save_orders_file_wrapper = lambda: saved.append(True)

    strategy = SimpleNamespace(_trade_id=None)
    authority = monitor._refresh_live_broker_authority(strategy)

    assert authority is not None
    assert near.status is OrderStatus.FILLED
    assert far.status is OrderStatus.FILLED
    assert saved == [True]
