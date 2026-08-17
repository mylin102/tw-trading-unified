"""RED/GREEN tests for the Phase 2 broker lifecycle decision contract."""
from core.order_lifecycle_reconciler import (
    LifecycleDecision,
    reconcile_order,
)


def _local(**overrides):
    value = {
        "order_id": "ORD-1", "symbol": "TMFI6", "status": "pending_submit",
        "broker_order_id": "B-1", "ordno": "O-1", "seqno": "S-1",
    }
    value.update(overrides)
    return value


def _snap(*, positions=(), open_orders=(), trades=(), source="live_broker"):
    return {
        "source": source, "session_id": "sess-1", "captured_at": 1,
        "positions": list(positions), "open_orders": list(open_orders),
        "trades": list(trades),
    }


def test_pending_submit_is_retained_when_broker_order_present():
    d = reconcile_order(_local(), _snap(open_orders=[{
        "broker_order_id": "B-1", "ordno": "O-1", "seqno": "S-1",
        "status": "PendingSubmit",
    }]))
    assert d == LifecycleDecision("ORD-1", "PENDING_UNCONFIRMED", "RETAIN",
                                  "BROKER_ORDER_PRESENT", "B-1")


def test_position_present_retains_without_resubmit():
    d = reconcile_order(_local(), _snap(positions=[{"code": "TMFI6"}]))
    assert d.state == "PENDING_UNCONFIRMED"
    assert d.action == "RETAIN"
    assert d.reason == "BROKER_POSITION_PRESENT"


def test_explicit_terminal_trade_is_authoritative():
    d = reconcile_order(_local(), _snap(trades=[{
        "broker_order_id": "B-1", "ordno": "O-1", "seqno": "S-1",
        "status": "Filled",
    }]))
    assert d.state == "FILLED"
    assert d.action == "APPLY_TERMINAL"


def test_absent_order_and_position_is_broker_not_found():
    d = reconcile_order(_local(), _snap())
    assert d.state == "BROKER_NOT_FOUND"
    assert d.action == "MARK_TERMINAL"


def test_query_failure_retains_and_never_terminalizes():
    d = reconcile_order(_local(), {"source": "unavailable", "capture_error": True})
    assert d.state == "RECONCILE_REQUIRED"
    assert d.action == "RETAIN"


def test_missing_identity_retains_and_never_matches_by_symbol_only():
    d = reconcile_order({"order_id": "ORD-1", "symbol": "TMFI6"}, _snap(
        positions=[{"code": "TMFI6"}],
    ))
    assert d.state == "RECONCILE_REQUIRED"
    assert d.reason == "BROKER_IDENTITY_MISSING"


def test_repeated_same_snapshot_is_deterministic():
    snap = _snap(open_orders=[{"broker_order_id": "B-1", "status": "Submitted"}])
    assert reconcile_order(_local(), snap) == reconcile_order(_local(), snap)

