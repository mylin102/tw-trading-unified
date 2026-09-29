"""Broker trade snapshots whose deals carry NO deal identity must still land.

Incident anchor (2026-09-29, trade broker-reconciled-b44e20763a980f92):
the combined-exit legs ORD-20260929-000246 (TMFJ6 BUY) and
ORD-20260929-000247 (TMFK6 SELL) were Filled at the broker (48063 / 48206),
but every `live_broker_reconcile` pass logged

    deal_fill_rejected | reason=BROKER_RECEIPT_IDENTITY_MISSING | px=48063 qty=1

because the refreshed trade row's deals carry price/quantity only - no
deal_id / trade_id / exchange_seq.  Consequence: `filled_quantity` stayed 0,
no RELEASE/EXIT row reached logs/mts_trade_fills.jsonl, and the realized PnL
for the trade stayed 0.0 while the broker had already filled both legs.

The identity must be derived deterministically from broker-provided content
(same shape as the existing twins: `broker-position:{...}` for position rows,
`_build_combo_fill_identity()` for combo rows), and a REJECTED deal must never
be counted as an applied fill.
"""
from core.order_management.order import OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager

BROKER_ORDER_ID = "0185be54"
ORDNO = "va05e"
SEQNO = "705542"
FILL_PRICE = 48063.0


def _trade_row(*, price=FILL_PRICE, quantity=1):
    """Shape observed in the live refresh row (list_trades) for the incident.

    `deals` intentionally carries ONLY price/quantity: no deal_id, no
    trade_id, no exchange_seq - exactly what the broker returned.
    """
    return {
        "id": BROKER_ORDER_ID,
        "broker_order_id": BROKER_ORDER_ID,
        "ordno": ORDNO,
        "seqno": SEQNO,
        "code": "TMFJ6",
        "direction": "buy",
        "status": "Filled",
        "broker_status": "Filled",
        "quantity": quantity,
        "filled_qty": quantity,
        "filled_quantity": quantity,
        "deals": [{"price": price, "quantity": quantity}],
    }


def _submitted_order(manager):
    order = manager.create_order(
        symbol="TMFJ6", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_EXIT",
    )
    order.submit(BROKER_ORDER_ID, broker_order_id=BROKER_ORDER_ID,
                 seqno=SEQNO, ordno=ORDNO)
    return order


def test_broker_trade_without_deal_identity_lands_the_fill():
    """RED at 5ca2aab1: the deal is rejected -> filled_quantity stays 0."""
    manager = OrderManager(mode="live")
    order = _submitted_order(manager)

    result = manager.reconcile_broker_state(
        filled_trades=[_trade_row()], source="live_broker_reconcile")

    assert order.filled_quantity == 1
    assert order.avg_fill_price == FILL_PRICE
    assert order.status is OrderStatus.FILLED
    assert result["reconciled"][0]["fills_added"] == 1


def test_broker_trade_without_deal_identity_is_idempotent():
    """Replaying the same broker row must not double-book the fill."""
    manager = OrderManager(mode="live")
    order = _submitted_order(manager)

    manager.reconcile_broker_state(
        filled_trades=[_trade_row()], source="live_broker_reconcile")
    manager.reconcile_broker_state(
        filled_trades=[_trade_row()], source="live_broker_reconcile")

    assert order.filled_quantity == 1
    assert len(order.fills) == 1


def test_rejected_deal_is_not_counted_as_applied(monkeypatch):
    """RED at 5ca2aab1: fills_added counts the deal even when it is rejected."""
    manager = OrderManager(mode="live")
    order = _submitted_order(manager)
    monkeypatch.setattr(manager, "apply_deal_fill", lambda *a, **k: None)

    result = manager.reconcile_broker_state(
        filled_trades=[_trade_row()], source="live_broker_reconcile")

    assert order.filled_quantity == 0
    assert result["reconciled"][0]["fills_added"] == 0
