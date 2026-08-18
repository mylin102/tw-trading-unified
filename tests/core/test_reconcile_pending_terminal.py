"""RED — auto terminal-inference wiring: the runtime snapshot reconcile
must mark phantom pending orders terminal.  reconcile_order was only
reachable via scripts/reconcile_pending_orders.py (restart-time), so a
pending order whose broker evidence disappeared (order + position both
absent — e.g. Shioaji session-cache residuals, callback gap) stayed
pending_submit forever.  This slice wires the decision into the periodic
live snapshot reconcile (fail-closed: RETAIN everywhere else).
"""
from core.order_management.order import (
    Order, OrderSide, OrderStatus, OrderType)
from core.order_management.order_manager import OrderManager
from strategies.futures.monitor import FuturesMonitor


def _order(order_id="ORD-1", status=OrderStatus.PENDING_SUBMIT,
           broker="cfafb0a7", symbol="TMFI6", strategy="MTS_EXIT"):
    o = Order(symbol=symbol, side=OrderSide.SELL, order_type=OrderType.MKP,
              quantity=1, strategy=strategy, order_id=order_id)
    o.broker_order_id = broker
    o.status = status
    return o


def _snapshot(positions=None, open_orders=None, trades=None,
              source="live_broker", capture_ok=True):
    snap = {
        "source": source,
        "fetch_status": {"capture": "OK" if capture_ok else "FAIL"},
        "positions": list(positions or []),
        "open_orders": list(open_orders or []),
        "trades": list(trades or []),
    }
    if not capture_ok:
        snap["capture_error"] = "boom"
    return snap


def _shell_rows():
    """Shioaji session-cache residual: no identity, no fields."""
    return [{"ordno": "", "code": None, "action": None, "quantity": None,
             "status": "PendingSubmit"}]


def _mgr(*orders):
    m = OrderManager(mode="live")
    for o in orders:
        m.active_orders[o.order_id] = o
    return m


def test_phantom_pending_marked_broker_not_found():
    """Order + position both absent at the broker (only shell residuals in
    the snapshot) -> the phantom pending is terminal (BROKER_NOT_FOUND)."""
    mgr = _mgr(_order())
    snap = _snapshot(positions=[], open_orders=_shell_rows(),
                     trades=_shell_rows())
    changed = mgr.reconcile_pending_terminal(snap)
    assert len(changed) == 1 and changed[0]["action"] == "MARK_TERMINAL"
    assert len(mgr.completed) == 1
    assert mgr.completed[0].status == OrderStatus.BROKER_NOT_FOUND
    assert "ORD-1" not in mgr.active_orders


def test_phantom_pending_retained_when_position_present():
    mgr = _mgr(_order())
    snap = _snapshot(positions=[{"code": "TMFI6", "quantity": 1}],
                     open_orders=_shell_rows(), trades=_shell_rows())
    assert mgr.reconcile_pending_terminal(snap) == []
    assert mgr.active_orders["ORD-1"].status == OrderStatus.PENDING_SUBMIT


def test_phantom_pending_retained_when_real_open_order_matches():
    """A REAL (identity + fields) open order row keeps the order pending —
    the broker says it is still working."""
    mgr = _mgr(_order())
    snap = _snapshot(positions=[],
                     open_orders=[{"ordno": "cfafb0a7", "code": "TMFI6",
                                   "action": "Sell", "quantity": 1,
                                   "status": "PendingSubmit"}])
    assert mgr.reconcile_pending_terminal(snap) == []
    assert mgr.active_orders["ORD-1"].status == OrderStatus.PENDING_SUBMIT


def test_invalid_snapshot_fails_closed():
    for snap in (_snapshot(source="paper"),
                 _snapshot(capture_ok=False)):
        mgr = _mgr(_order())
        assert mgr.reconcile_pending_terminal(snap) == []
        assert mgr.active_orders["ORD-1"].status == OrderStatus.PENDING_SUBMIT


def test_non_pending_orders_untouched():
    mgr = _mgr(
        _order("ORD-S", status=OrderStatus.SUBMITTED),
        _order("ORD-F", status=OrderStatus.FILLED),
        _order("ORD-C", status=OrderStatus.CANCELLED),
    )
    snap = _snapshot(positions=[], open_orders=_shell_rows(),
                     trades=_shell_rows())
    assert mgr.reconcile_pending_terminal(snap) == []
    assert mgr.active_orders["ORD-S"].status == OrderStatus.SUBMITTED
    assert mgr.active_orders["ORD-F"].status == OrderStatus.FILLED
    assert mgr.active_orders["ORD-C"].status == OrderStatus.CANCELLED


def test_explicit_broker_terminal_state_applied():
    """A matched open-order row with an explicit terminal state (Cancelled)
    maps onto the OrderStatus terminal member (APPLY_TERMINAL)."""
    mgr = _mgr(_order())
    snap = _snapshot(positions=[],
                     open_orders=[{"ordno": "cfafb0a7", "code": "TMFI6",
                                   "action": "Sell", "quantity": 1,
                                   "status": "Cancelled"}])
    changed = mgr.reconcile_pending_terminal(snap)
    assert len(changed) == 1 and changed[0]["action"] == "APPLY_TERMINAL"
    assert mgr.completed[0].status == OrderStatus.CANCELLED


def test_monitor_snapshot_reconcile_invokes_pending_terminal():
    """The monitor's periodic snapshot reconcile must invoke the terminal
    inference and persist when something changed."""
    calls = []

    class _Mgr:
        def reconcile_broker_state(self, *a, **kw):
            return {"reconciled": [], "unmatched": []}

        def reconcile_position_covered_orders(self, *a, **kw):
            return {"reconciled": []}

        def reconcile_pending_terminal(self, *a, **kw):
            calls.append(a[0] if a else kw.get("snapshot"))
            return [{"order_id": "ORD-1", "action": "MARK_TERMINAL"}]

    m = FuturesMonitor.__new__(FuturesMonitor)
    m.order_mgr = _Mgr()
    m._save_orders_file_wrapper = lambda: None
    snap = _snapshot()
    n = m._reconcile_local_orders_from_snapshot(snap)
    assert calls and calls[0] is snap
    assert n == 1
