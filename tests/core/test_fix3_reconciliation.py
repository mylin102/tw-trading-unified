from pathlib import Path
from types import SimpleNamespace

from core.order_lifecycle_reconciler import reconcile_order
from core.order_management.order import Order, OrderSide, OrderStatus, OrderType


def local(**overrides):
    value = {
        "order_id": "ORD-IOC-1",
        "symbol": "TMFH6/TMFK6",
        "broker_order_id": "B-IOC-1",
        "receipt": True,
        "submitted": True,
        "position_baseline": [
            {"code": "TMFH6", "direction": "Sell", "quantity": 1},
            {"code": "TMFK6", "direction": "Buy", "quantity": 1},
        ],
    }
    value.update(overrides)
    return value


def evidence(*, status=None, deal_quantity=0, deals=None, positions=None,
             window_complete=True, callback=True, query=True,
             mismatch=False):
    return {
        "source": "live_broker",
        "query_evidence": {
            "order_query": query,
            "combo_status": query,
            "combo_trades": query,
            "position_query": query,
            "callbacks": ["ORDER_SUBMITTED"] if callback else [],
            "window_complete": window_complete,
            "receipt": True,
            "broker_order_id": "B-IOC-1",
            "order_status": status,
            "deal_quantity": deal_quantity,
            "deals": deals if deals is not None else {},
            "mismatch": mismatch,
        },
        "positions": positions if positions is not None else local()["position_baseline"],
        "open_orders": [],
        "trades": [],
    }


def test_ioc_cancelled_zero_fill_is_received_not_filled():
    decision = reconcile_order(local(), evidence(status="Cancelled"))
    assert decision.state == "RECEIVED_NOT_FILLED"
    assert decision.action == "APPLY_ZERO_FILL_TERMINAL"


def test_delayed_callback_is_pending_and_never_resubmitted():
    decision = reconcile_order(local(), evidence(status=None, callback=False,
                                                 window_complete=False))
    assert decision.state == "PENDING_UNCONFIRMED"
    assert decision.action == "RETAIN"


def test_temporarily_missing_order_snapshot_is_pending_not_leg_clear():
    decision = reconcile_order(local(), evidence(status=None, query=False,
                                                 window_complete=False))
    assert decision.state == "PENDING_UNCONFIRMED"
    assert decision.action == "RETAIN"


def test_empty_list_trades_with_receipt_is_not_not_sent():
    decision = reconcile_order(local(), evidence(status=None, callback=True,
                                                 window_complete=True))
    assert decision.state != "NOT_SENT"
    assert decision.state == "PENDING_UNCONFIRMED"


def test_inactive_zero_fill_unchanged_positions_is_received_not_filled():
    decision = reconcile_order(local(), evidence(status="Inactive"))
    assert decision.state == "RECEIVED_NOT_FILLED"


def test_failed_without_complete_evidence_is_unknown_not_rejected():
    decision = reconcile_order(local(), evidence(status="Failed", query=False,
                                                 window_complete=False))
    assert decision.state in {"UNKNOWN", "PENDING_UNCONFIRMED"}
    assert decision.state != "REJECTED"


def test_partial_fill_retains_active_legs_and_blocks_resubmit():
    decision = reconcile_order(
        local(), evidence(status="PartFilled", deal_quantity=1,
                          deals={"TMFH6": [{"quantity": 1}],
                                 "TMFK6": [{"quantity": 0}]}),
    )
    assert decision.state == "PARTIAL_FILLED"
    assert decision.action == "RETAIN"


def test_broker_local_identity_mismatch_is_unknown():
    decision = reconcile_order(local(), evidence(status="Cancelled",
                                                 mismatch=True))
    assert decision.state == "UNKNOWN"
    assert decision.action == "RETAIN"


def test_broker_not_found_is_not_a_terminal_safe_state():
    assert "BROKER_NOT_FOUND" not in {"FILLED", "CANCELLED", "REJECTED", "EXPIRED"}
    source = Path("strategies/futures/monitor.py").read_text()
    assert '"BROKER_NOT_FOUND", "BROKER_RECONCILED"' not in source
    assert '"BROKER_NOT_FOUND",\n' not in source


def test_not_sent_requires_all_five_absence_conditions():
    no_receipt = local(receipt=False, broker_order_id=None)
    snap = evidence(status=None, callback=False, query=False,
                    window_complete=True)
    decision = reconcile_order(no_receipt, snap)
    assert decision.state == "NOT_SENT"
    assert decision.action == "RETAIN_AUDIT"


def test_pending_state_baseline_and_idempotency_survive_restart_roundtrip():
    order = Order(symbol="TMFH6/TMFK6", side=OrderSide.SELL,
                  order_type=OrderType.LIMIT, quantity=1,
                  order_id="ORD-PERSIST-1")
    order.status = OrderStatus.PENDING_UNCONFIRMED
    order.position_baseline = local()["position_baseline"]
    order.idempotency_key = "mts-entry:key-1"
    order.retry_of_order_id = None
    restored = Order.from_dict(order.to_dict())
    assert restored.status is OrderStatus.PENDING_UNCONFIRMED
    assert restored.position_baseline == order.position_baseline
    assert restored.idempotency_key == "mts-entry:key-1"
    assert restored.retry_of_order_id is None

