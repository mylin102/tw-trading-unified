from datetime import datetime, timedelta

from core.order_management.order import Order, OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager
from strategies.futures.monitor import FuturesMonitor


class _WatchdogMonitor(FuturesMonitor):
    def __init__(self, snapshot):
        self._snapshot = snapshot
        self.events = []

    def _capture_post_startup_snapshot(self):
        return self._snapshot

    def _append_mts_event(self, event_type, **payload):
        self.events.append((event_type, payload))


def _order(manager, order_id="ORD-1"):
    order = Order("TMFI6", OrderSide.BUY, OrderType.MKP, 1,
                  order_id=order_id, strategy="MTS_ENTRY")
    manager.active_orders[order_id] = order
    order.submit("BROKER-1", broker_order_id="BROKER-1", ordno="BROKER-1")
    return order


def _snapshot(*, capture="OK", open_orders=None, positions=None):
    return {
        "source": "live_broker",
        "fetch_status": {"capture": capture},
        "open_orders": open_orders or [],
        "positions": positions or [],
    }


def test_pending_broker_order_protects_watchdog_without_cancel():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    monitor = _WatchdogMonitor(_snapshot(open_orders=[
        {"broker_order_id": "BROKER-1", "ordno": "BROKER-1",
         "code": "TMFI6", "status": "PendingSubmit"}
    ]))
    monitor.order_mgr = manager
    truth = monitor._watchdog_broker_truth(order)
    assert truth["protect"] is True
    assert truth["reason"] == "BROKER_HAS_POSITION_OR_ORDER"
    assert manager.active_orders[order.order_id].status is OrderStatus.SUBMITTED
    assert monitor.events[0][0] == "WATCHDOG_BROKER_HAS_POSITION_OR_ORDER"


def test_matching_position_protects_watchdog_without_terminal_transition():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    monitor = _WatchdogMonitor(_snapshot(positions=[
        {"code": "TMFI6", "quantity": 1, "direction": "Buy",
         "avg_cost": 46329}
    ]))
    monitor.order_mgr = manager
    assert monitor._watchdog_broker_truth(order)["protect"] is True
    assert order.status is OrderStatus.SUBMITTED


def test_capture_failure_protects_and_never_cancels():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    monitor = _WatchdogMonitor(_snapshot(capture="FAIL"))
    monitor.order_mgr = manager
    truth = monitor._watchdog_broker_truth(order)
    assert truth["protect"] is True
    assert truth["reason"] == "BROKER_QUERY_UNAVAILABLE"
    assert order.status is OrderStatus.SUBMITTED
    assert monitor.events[0][0] == "WATCHDOG_BROKER_QUERY_UNAVAILABLE"


def test_successful_empty_read_is_not_a_terminal_receipt():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    monitor = _WatchdogMonitor(_snapshot())
    monitor.order_mgr = manager
    truth = monitor._watchdog_broker_truth(order)
    assert truth["protect"] is True
    assert truth["reason"] == "BROKER_NO_POSITION_OR_ORDER"
    assert order.status is OrderStatus.SUBMITTED
    assert monitor.events[0][0] == "WATCHDOG_BROKER_NO_POSITION_OR_ORDER"


def test_terminal_order_is_restored_only_from_broker_truth_without_resubmit():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    manager.expire(order.order_id, reason="local watchdog")
    assert order.status is OrderStatus.EXPIRED
    monitor = _WatchdogMonitor(_snapshot(open_orders=[
        {"broker_order_id": "BROKER-1", "ordno": "BROKER-1",
         "code": "TMFI6", "status": "PendingSubmit"}
    ]))
    monitor.order_mgr = manager
    truth = monitor._watchdog_broker_truth(order)
    assert monitor._restore_terminal_watchdog_order(order, truth) is True
    assert manager.active_orders[order.order_id] is order
    assert order.status is OrderStatus.SUBMITTED
    assert not manager.completed


def test_terminal_broker_receipt_remains_authoritative():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    manager.apply_order_update(order.order_id, raw_status="Cancelled",
                               broker_order_id="BROKER-1",
                               source="broker_callback")
    assert order.status is OrderStatus.CANCELLED
    assert order.order_id not in manager.active_orders


def test_paper_orders_are_not_broker_protected_by_missing_capture():
    manager = OrderManager(mode="paper")
    order = _order(manager)
    monitor = _WatchdogMonitor(_snapshot(capture="FAIL"))
    monitor.order_mgr = manager
    # The guard is conservative for paper too, but it must not mutate or
    # submit/cancel; callers may choose paper-specific timeout policy.
    truth = monitor._watchdog_broker_truth(order)
    assert truth["protect"] is True
    assert order.status is OrderStatus.SUBMITTED
