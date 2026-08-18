from types import SimpleNamespace

from core.order_management.order import OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager


def _submitted(manager, symbol, side, broker_id):
    order = manager.create_order(symbol, side, OrderType.MARKET, 1)
    manager.attach_submission(
        order.order_id,
        broker_order_id=broker_id,
        ordno=broker_id,
        raw_status="Submitted",
        source="broker_submit",
    )
    return order


def test_broker_snapshot_terminal_trade_backfills_local_order():
    manager = OrderManager(mode="live")
    order = _submitted(manager, "TMFH6", OrderSide.SELL, "BRK-NEAR")

    result = manager.reconcile_broker_state(
        filled_trades=[{
            "id": "BRK-NEAR",
            "ordno": "BRK-NEAR",
            "status": "Filled",
            "filled_qty": 1,
            "deals": [],
        }],
        source="live_broker_reconcile",
        reason="callback_gap_snapshot",
    )

    assert result["reconciled"]
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 1
    assert order.avg_fill_price == 0.0
    assert order.fill_accounting_status == "DETAILS_PENDING"
    assert order in manager.get_completed()


def test_broker_snapshot_uses_nested_order_identity():
    """Shioaji Trade may expose the broker id as ``trade.order.id`` only."""
    from strategies.futures.monitor import FuturesMonitor

    manager = OrderManager(mode="live")
    order = _submitted(manager, "TMFH6", OrderSide.SELL, "BRK-NESTED")
    raw_trade = SimpleNamespace(
        order=SimpleNamespace(id="BRK-NESTED"),
        code="TMFH6",
        status=SimpleNamespace(status="Filled", price=46411.0,
                                quantity=1, deals=[]),
    )

    normalized = FuturesMonitor._normalize_snapshot_trades([raw_trade])
    assert normalized[0]["id"] == "BRK-NESTED"
    result = manager.reconcile_broker_state(
        filled_trades=normalized,
        source="live_broker_reconcile",
        reason="callback_gap_snapshot",
    )

    assert result["reconciled"]
    assert order.status is OrderStatus.FILLED


def test_session_close_finalizes_only_active_orders_and_preserves_fills():
    manager = OrderManager(mode="live")
    pending = _submitted(manager, "TMFI6", OrderSide.BUY, "BRK-PENDING")
    partial = manager.create_order("TMFH6", OrderSide.SELL, OrderType.MARKET, 2)
    manager.attach_submission(
        partial.order_id, broker_order_id="BRK-PARTIAL", raw_status="Submitted"
    )
    manager.apply_deal_fill(
        partial.order_id,
        fill_price=46411.0,
        fill_qty=1,
        deal_id="DEAL-1",
        broker_order_id="BRK-PARTIAL",
        source="live_broker",
    )

    finalized = manager.finalize_session_orders(
        source="session_close_reconcile",
        reason="BROKER_SESSION_CLOSED_UNFILLED",
    )

    assert finalized == 2
    assert pending.status is OrderStatus.EXPIRED
    assert partial.status is OrderStatus.CANCELLED
    assert partial.filled_quantity == 1
    assert len(partial.fills) == 1
    assert manager.get_pending() == []


class _FakeApi:
    futopt_account = SimpleNamespace(account_id="futopt-1")
    stock_account = None

    def list_positions(self, account=None):
        return [
            SimpleNamespace(code="TMFH6", direction="Action.Sell", quantity=1,
                            price=46411.0, pnl=-220.0),
            SimpleNamespace(code="TMFI6", direction="Action.Buy", quantity=1,
                            price=46569.0, pnl=250.0),
        ]

    def update_status(self, account=None):
        return None

    def list_trades(self, account=None):
        return [
            SimpleNamespace(
                id="BRK-NEAR", ordno="BRK-NEAR", seqno="SEQ-1",
                code="TMFH6",
                status=SimpleNamespace(status="Filled", price=46411.0,
                                        quantity=1, order_quantity=1,
                                        deal_quantity=1, deals=[SimpleNamespace(
                                            price=46411.0, quantity=1,
                                            exchange_seq="XS-NEAR")]),
            ),
            SimpleNamespace(
                id="BRK-FAR", ordno="BRK-FAR", seqno="SEQ-2",
                code="TMFI6",
                status=SimpleNamespace(status="Filled", price=46569.0,
                                        quantity=1, order_quantity=1,
                                        deal_quantity=1, deals=[SimpleNamespace(
                                            price=46569.0, quantity=1,
                                            exchange_seq="XS-FAR")]),
            ),
        ]

    def margin(self, account=None):
        return SimpleNamespace(available_margin=300000.0)


def test_monitor_refresh_reconciles_broker_trades_and_exports(monkeypatch):
    from strategies.futures.monitor import FuturesMonitor

    manager = OrderManager(mode="live")
    near = _submitted(manager, "TMFH6", OrderSide.SELL, "BRK-NEAR")
    far = _submitted(manager, "TMFI6", OrderSide.BUY, "BRK-FAR")
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor.api = _FakeApi()
    monitor.order_mgr = manager
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._execution_context = SimpleNamespace(
        requested_mode="live", effective_mode="live_ready",
        session_id="session-1"
    )
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
    assert near.avg_fill_price == 46411.0
    assert far.avg_fill_price == 46569.0
    assert saved == [True]


def test_monitor_session_close_uses_empty_broker_open_orders(monkeypatch):
    from strategies.futures.monitor import FuturesMonitor

    manager = OrderManager(mode="live")
    order = _submitted(manager, "TMFH6", OrderSide.SELL, "BRK-CLOSE")
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.dry_run = False
    monitor.live_trading = True
    monitor.order_mgr = manager
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._session_close_finalized_for = None
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "OK"},
        "open_orders": [],
    }
    saved = []
    monitor._save_orders_file_wrapper = lambda: saved.append(True)

    finalized = monitor._finalize_local_orders_at_session_close()

    assert finalized == 1
    assert order.status is OrderStatus.EXPIRED
    assert saved == [True]


def test_monitor_session_close_does_not_expire_when_broker_order_remains():
    from strategies.futures.monitor import FuturesMonitor

    manager = OrderManager(mode="live")
    order = _submitted(manager, "TMFH6", OrderSide.SELL, "BRK-STILL-OPEN")
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.dry_run = False
    monitor.live_trading = True
    monitor.order_mgr = manager
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._session_close_finalized_for = None
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "OK"},
        "open_orders": [{"code": "TMFH6", "status": "Submitted"}],
    }

    assert monitor._finalize_local_orders_at_session_close() == 0
    assert order.status is OrderStatus.SUBMITTED
    assert manager.get_pending() == [order]
