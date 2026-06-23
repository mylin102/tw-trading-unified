from types import SimpleNamespace

from core.order_management.order import OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager


def _latest_event_of_type(order, event_type):
    for event in reversed(order.raw_events):
        if event.get("type") == event_type:
            return event
    raise AssertionError(f"missing event type={event_type}")


def _assert_normalized_audit(event, *, source, reason, to_status):
    assert event["timestamp"]
    assert event["source"] == source
    assert event["reason"] == reason
    assert event["to_status"] == to_status
    assert "from_status" in event


def test_reconcile_transition_records_source_reason_and_timestamp():
    mgr = OrderManager(mode="paper")
    order = mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)

    mgr.attach_submission(
        order.order_id,
        broker_order_id="BROKER-101",
        ordno="ORDNO-101",
        seqno="SEQ-101",
        raw_status="Submitted",
        source="broker_submit",
        reason="initial_submit",
    )
    submission_event = _latest_event_of_type(order, "submission")
    _assert_normalized_audit(
        submission_event,
        source="broker_submit",
        reason="initial_submit",
        to_status=OrderStatus.SUBMITTED.value,
    )

    mgr.apply_order_update(
        order.order_id,
        raw_status="PartFilled",
        source="callback",
        reason="order_callback",
        raw_payload={"status": "PartFilled"},
    )
    update_event = _latest_event_of_type(order, "order_update")
    _assert_normalized_audit(
        update_event,
        source="callback",
        reason="order_callback",
        to_status=OrderStatus.PARTIAL_FILLED.value,
    )

    trade = SimpleNamespace(
        id="BROKER-101",
        ordno="ORDNO-101",
        seqno="SEQ-101",
        status=SimpleNamespace(
            status="Filled",
            deals=[SimpleNamespace(price=36455, quantity=1, trade_id="TRD-101", exchange_seq="XS-101", ordno="ORDNO-101")],
        ),
    )
    mgr.reconcile_trade_snapshot(
        order.order_id,
        trade=trade,
        source="broker_refresh",
        reason="callback_gap_refresh",
    )

    reconcile_event = _latest_event_of_type(order, "reconcile")
    _assert_normalized_audit(
        reconcile_event,
        source="broker_refresh",
        reason="callback_gap_refresh",
        to_status=OrderStatus.FILLED.value,
    )
    deal_event = _latest_event_of_type(order, "deal_fill")
    _assert_normalized_audit(
        deal_event,
        source="broker_refresh",
        reason="callback_gap_refresh",
        to_status=OrderStatus.FILLED.value,
    )


def test_cancel_reject_expire_and_recovery_record_normalized_audit_rows():
    mgr = OrderManager(mode="paper")

    cancel_order = mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
    mgr.attach_submission(cancel_order.order_id, raw_status="Submitted")
    mgr.cancel(cancel_order.order_id, reason="user_cancel", source="operator")
    cancel_event = _latest_event_of_type(cancel_order, "cancel")
    _assert_normalized_audit(
        cancel_event,
        source="operator",
        reason="user_cancel",
        to_status=OrderStatus.CANCELLED.value,
    )

    reject_order = mgr.create_order("TMF", OrderSide.SELL, OrderType.MARKET, 1)
    mgr.reject(reject_order.order_id, "risk_block", source="risk_engine")
    reject_event = _latest_event_of_type(reject_order, "reject")
    _assert_normalized_audit(
        reject_event,
        source="risk_engine",
        reason="risk_block",
        to_status=OrderStatus.REJECTED.value,
    )

    expire_order = mgr.create_order("TMF", OrderSide.BUY, OrderType.LIMIT, 1, price=36400)
    mgr.attach_submission(expire_order.order_id, raw_status="Submitted")
    mgr.expire(expire_order.order_id, source="session_close", reason="market_closed")
    expire_event = _latest_event_of_type(expire_order, "expire")
    _assert_normalized_audit(
        expire_event,
        source="session_close",
        reason="market_closed",
        to_status=OrderStatus.EXPIRED.value,
    )

    recovered = mgr.recover_from_api(
        filled_trades=[SimpleNamespace(ordno="RECOV-001", symbol="TMF", action="Buy", quantity=1, price=36420)],
        open_orders=[SimpleNamespace(ordno="RECOV-002", symbol="TMF", action="Sell", quantity=1, price=36510)],
        source="startup_recovery",
        reason="recover_from_api",
    )

    assert recovered == {"filled": 1, "open": 1, "failed": 0}
    recovered_filled = next(order for order in mgr.get_completed() if order.order_id == "RECOV-RECOV-001")
    recovered_open = mgr.active_orders["RECOV-RECOV-002"]

    recovery_filled_event = _latest_event_of_type(recovered_filled, "recovery")
    _assert_normalized_audit(
        recovery_filled_event,
        source="startup_recovery",
        reason="recover_from_api",
        to_status=OrderStatus.FILLED.value,
    )
    recovery_open_event = _latest_event_of_type(recovered_open, "recovery")
    _assert_normalized_audit(
        recovery_open_event,
        source="startup_recovery",
        reason="recover_from_api",
        to_status=OrderStatus.SUBMITTED.value,
    )
