from types import SimpleNamespace

from core.order_management.order import OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager


def _make_deal(*, price, quantity, trade_id=None, exchange_seq=None, ordno="ORDNO-001", seqno="SEQ-001"):
    return SimpleNamespace(
        price=price,
        quantity=quantity,
        trade_id=trade_id,
        exchange_seq=exchange_seq,
        ordno=ordno,
        seqno=seqno,
    )


def _make_trade(*, broker_order_id="BROKER-001", ordno="ORDNO-001", seqno="SEQ-001", status="Filled", deals=None):
    return SimpleNamespace(
        id=broker_order_id,
        ordno=ordno,
        seqno=seqno,
        status=SimpleNamespace(status=status, deals=deals or []),
    )


def test_reconcile_trade_snapshot_backfills_missing_fill():
    mgr = OrderManager(mode="paper")
    order = mgr.create_order("TMF", OrderSide.BUY, OrderType.LIMIT, 2, price=36400)
    mgr.attach_submission(
        order.order_id,
        broker_order_id="BROKER-001",
        ordno="ORDNO-001",
        seqno="SEQ-001",
        raw_status="Submitted",
        source="callback",
        reason="submit_ack",
    )

    trade = _make_trade(
        status="Filled",
        deals=[_make_deal(price=36395, quantity=2, trade_id="TRD-001", exchange_seq="XS-001")],
    )

    result = mgr.reconcile_trade_snapshot(
        order.order_id,
        trade=trade,
        source="broker_refresh",
        reason="callback_gap_refresh",
    )

    assert result["matched"] is True
    assert result["fills_added"] == 1
    assert order.status == OrderStatus.FILLED
    assert order.filled_quantity == 2
    assert order.avg_fill_price == 36395
    assert len(order.fills) == 1
    assert order.fills[0].deal_id == "TRD-001"
    assert order in mgr.get_completed()

    replay = mgr.reconcile_trade_snapshot(
        order.order_id,
        trade=trade,
        source="broker_refresh",
        reason="callback_gap_refresh",
    )
    assert replay["fills_added"] == 0
    assert len(order.fills) == 1


def test_reconcile_trade_snapshot_dedupes_by_canonical_fill_ids_not_ordno():
    mgr = OrderManager(mode="paper")
    order = mgr.create_order("TMF", OrderSide.SELL, OrderType.MARKET, 2)
    mgr.attach_submission(
        order.order_id,
        broker_order_id="BROKER-002",
        ordno="ORDNO-SHARED",
        seqno="SEQ-002",
        raw_status="Submitted",
    )

    trade = _make_trade(
        broker_order_id="BROKER-002",
        ordno="ORDNO-SHARED",
        seqno="SEQ-002",
        status="Filled",
        deals=[
            _make_deal(price=36510, quantity=1, trade_id="TRD-201", exchange_seq="XS-201", ordno="ORDNO-SHARED"),
            _make_deal(price=36520, quantity=1, trade_id="TRD-202", exchange_seq="XS-202", ordno="ORDNO-SHARED"),
        ],
    )

    result = mgr.reconcile_trade_snapshot(
        order.order_id,
        trade=trade,
        source="broker_refresh",
        reason="missing_fill_backfill",
    )

    assert result["fills_added"] == 2
    assert [fill.deal_id for fill in order.fills] == ["TRD-201", "TRD-202"]
    assert order.status == OrderStatus.FILLED


def test_reconcile_trade_snapshot_unmatched_snapshot_returns_safe_signal():
    mgr = OrderManager(mode="paper")
    order = mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)

    result = mgr.reconcile_trade_snapshot(
        trade=_make_trade(broker_order_id="BROKER-999", ordno="ORDNO-999", status="Submitted"),
        source="broker_refresh",
        reason="callback_gap_refresh",
    )

    assert result == {
        "matched": False,
        "action": "unmatched_snapshot",
        "order_id": None,
        "fills_added": 0,
    }
    assert list(mgr.active_orders) == [order.order_id]
    assert mgr.completed == []

