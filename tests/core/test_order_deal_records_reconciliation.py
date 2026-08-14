"""Historical Shioaji order_deal_records must reconcile terminal fills."""
from types import SimpleNamespace

from core.order_management.order import OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager
from strategies.futures.monitor import FuturesMonitor


def _deal_record(*, trade_id="BROKER-1", delivery_month="202609",
                 action="Sell", price=46016.0, quantity=1):
    state = SimpleNamespace(name="FuturesDeal", value="FDEAL")
    payload = {
        "code": "TMF", "full_code": "", "delivery_month": delivery_month,
        "trade_id": trade_id, "exchange_seq": "EX-1", "seqno": "SEQ-1",
        "ordno": "ORD-1", "action": action, "price": price,
        "quantity": quantity, "ts": 1786690800.0,
    }
    return state, payload


def test_nested_futures_deal_normalizes_to_terminal_receipt():
    rows = FuturesMonitor._normalize_order_deal_records([_deal_record()])
    assert len(rows) == 1
    row = rows[0]
    assert row["id"] == "BROKER-1"
    assert row["status"] == "Filled"
    assert row["delivery_month"] == "202609"
    assert row["deals"][0]["exchange_seq"] == "EX-1"
    assert row["deals"][0]["price"] == 46016.0


def test_nested_deal_reconciles_existing_order_once():
    manager = OrderManager(mode="live")
    order = manager.create_order(symbol="TMFI6", side=OrderSide.SELL,
                                 order_type=OrderType.MARKET, quantity=1,
                                 strategy="MTS_EXIT")
    order.submit("BROKER-1", broker_order_id="BROKER-1",
                 seqno="SEQ-1", ordno="ORD-1")
    rows = FuturesMonitor._normalize_order_deal_records([_deal_record()])
    first = manager.reconcile_broker_state(filled_trades=rows,
                                           source="order_deal_records")
    second = manager.reconcile_broker_state(filled_trades=rows,
                                            source="order_deal_records")
    assert len(first["reconciled"]) == 1
    assert first["reconciled"][0]["fills_added"] == 1
    assert second["reconciled"] == []
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 1
    assert order.avg_fill_price == 46016.0


def test_non_deal_order_state_is_not_a_terminal_fill():
    state = SimpleNamespace(name="FuturesOrder", value="FORDER")
    rows = FuturesMonitor._normalize_order_deal_records(
        [(state, {"status": {"id": "BROKER-1"},
                  "contract": {"delivery_month": "202609"}})])
    assert rows == []
