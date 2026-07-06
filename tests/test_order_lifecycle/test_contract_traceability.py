from core.order_management.order import Order, OrderSide, OrderStatus, OrderType
from core.order_management.order_fill import OrderFill


def test_order_round_trip_preserves_traceability():
    order = Order(
        symbol="TMF",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=2,
        price=36400,
        strategy="counter_vwap",
        account="paper",
        comment="traceability",
        order_id="ORD-TRACE",
        intent_id="intent-trace",
    )

    order.submit(
        "EXCH-001",
        broker_order_id="BROKER-001",
        seqno="SEQ-001",
        ordno="ORDNO-001",
    )
    order.fill(
        fill_price=36390,
        fill_quantity=1,
        commission=12.5,
        tax=4.0,
        deal_id="deal-001",
        exchange_fill_id="EXF-001",
        broker_trade_id="BT-001",
        exchange_seq="XS-001",
    )
    order.raw_events.append({"source": "callback", "status": "submitted"})

    payload = order.to_dict()
    rebuilt = Order.from_dict(payload)

    assert rebuilt.intent_id == "intent-trace"
    assert rebuilt.order_id == "ORD-TRACE"
    assert rebuilt.broker_order_id == "BROKER-001"
    assert rebuilt.seqno == "SEQ-001"
    assert rebuilt.ordno == "ORDNO-001"
    assert len(rebuilt.fills) == 1
    assert rebuilt.fills[0].deal_id == "deal-001"
    assert rebuilt.fills[0].fill_id == "deal-001"
    assert rebuilt.fills[0].exchange_fill_id == "EXF-001"
    assert rebuilt.fills[0].broker_trade_id == "BT-001"
    assert rebuilt.fills[0].exchange_seq == "XS-001"
    assert rebuilt.raw_events == [{"source": "callback", "status": "submitted"}]


def test_order_fill_to_dict_exposes_traceability_fields():
    fill = OrderFill(
        order_id="ORD-TRACE",
        fill_price=36390,
        fill_quantity=1,
        deal_id="deal-001",
        exchange_fill_id="EXF-001",
        broker_trade_id="BT-001",
        exchange_seq="XS-001",
    )

    payload = fill.to_dict()

    assert payload["deal_id"] == "deal-001"
    assert payload["fill_id"] == "deal-001"
    assert payload["order_id"] == "ORD-TRACE"
    assert payload["exchange_fill_id"] == "EXF-001"
    assert payload["broker_trade_id"] == "BT-001"
    assert payload["exchange_seq"] == "XS-001"


def test_order_to_dict_keeps_legacy_and_new_export_keys():
    order = Order(
        symbol="TMF",
        side=OrderSide.SELL,
        order_type=OrderType.MARKET,
        quantity=1,
        order_id="ORD-LEGACY",
        intent_id="intent-legacy",
    )
    order.submit("EXCH-LEGACY", broker_order_id="BROKER-LEGACY", ordno="ORDNO-LEGACY")
    order.status = OrderStatus.SUBMITTED

    payload = order.to_dict()

    assert payload["exchange_order_id"] == "EXCH-LEGACY"
    assert payload["broker_order_id"] == "BROKER-LEGACY"
    assert payload["ordno"] == "ORDNO-LEGACY"
    assert payload["intent_id"] == "intent-legacy"
    assert payload["fills"] == []
