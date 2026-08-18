from enum import Enum
import threading
import time


class _Status(Enum):
    Filled = "Filled"


class _Trade:
    def __init__(self):
        self.order = type("Order", (), {
            "id": "cfafb0a7", "seqno": 833095, "ordno": "A1",
            "quantity": 2, "action": "Sell"})()
        self.status = type("Status", (), {
            "id": "status-1", "status": _Status.Filled,
            "status_code": 200, "order_quantity": 2,
            "deal_quantity": 1, "cancel_quantity": 0,
            "deals": []})()
        self.contract = type("Contract", (), {"code": "TMFI6"})()


def test_trade_normalizer_uses_nested_shioaji_fields_and_provenance():
    from strategies.futures.monitor import FuturesMonitor

    row = FuturesMonitor._normalize_snapshot_trades(
        [_Trade()], observation_type="REFRESHED_TRADE",
        snapshot_generation="futures-1", observed_at=123)[0]
    assert row["broker_order_id"] == "cfafb0a7"
    assert row["seqno"] == 833095
    assert row["ordno"] == "A1"
    assert row["code"] == "TMFI6"
    assert row["requested_qty"] == 2
    assert row["filled_qty"] == 1
    assert row["cancelled_qty"] == 0
    assert row["broker_status"] == "Filled"
    assert row["observation_type"] == "REFRESHED_TRADE"
    assert row["snapshot_generation"] == "futures-1"


def _monitor_for_refresh(api):
    from strategies.futures.monitor import FuturesMonitor
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon._broker_refresh_lock = threading.Lock()
    mon._futures_refresh_generation = 0
    mon._futures_refresh_timeout_s = 0.05
    mon.api = api
    mon._append_mts_event = lambda *args, **kwargs: None
    return mon


def test_refresh_is_update_then_list_and_publishes_generation():
    class API:
        is_connected = True
        futopt_account = object()
        def __init__(self): self.calls = []
        def update_status(self, **kwargs): self.calls.append("update")
        def list_trades(self): self.calls.append("list"); return [_Trade()]

    api = API()
    result = _monitor_for_refresh(api)._refresh_futures_trade_view(
        api, api.futopt_account)
    assert result["state"] == "REFRESH_SUCCEEDED"
    assert api.calls == ["update", "list"]
    assert result["snapshot_generation"].startswith("futures-")


def test_refresh_failure_never_reads_or_publishes_old_trades():
    class API:
        is_connected = True
        futopt_account = object()
        def __init__(self): self.list_called = False
        def update_status(self, **kwargs): raise RuntimeError("offline")
        def list_trades(self): self.list_called = True; return [_Trade()]

    api = API()
    result = _monitor_for_refresh(api)._refresh_futures_trade_view(
        api, api.futopt_account)
    assert result["state"] == "REFRESH_EXCEPTION"
    assert result["rows"] == []
    assert api.list_called is False


def test_refresh_timeout_keeps_single_flight_lock_until_worker_finishes():
    class API:
        is_connected = True
        futopt_account = object()
        def update_status(self, **kwargs): time.sleep(0.12)
        def list_trades(self): return []

    api = API()
    mon = _monitor_for_refresh(api)
    first = mon._refresh_futures_trade_view(api, api.futopt_account)
    second = mon._refresh_futures_trade_view(api, api.futopt_account)
    assert first["state"] == "REFRESH_TIMEOUT"
    assert second["state"] == "REFRESH_SKIPPED_INFLIGHT"


def test_filled_without_deals_is_terminal_but_details_pending():
    from core.order_management.order import OrderSide, OrderStatus, OrderType
    from core.order_management.order_manager import OrderManager

    mgr = OrderManager(mode="paper")
    order = mgr.create_order("TMFI6", OrderSide.SELL, OrderType.MARKET, 2)
    mgr.attach_submission(order.order_id, broker_order_id="cfafb0a7",
                          seqno="833095", ordno="A1")
    result = mgr.reconcile_trade_snapshot(trade={
        "broker_order_id": "cfafb0a7", "seqno": "833095", "ordno": "A1",
        "broker_status": "Filled", "requested_qty": 2,
        "filled_qty": 2, "deals": []})
    assert result["matched"] is True
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 2
    assert order.avg_fill_price == 0.0
    assert order.fill_accounting_status == "DETAILS_PENDING"
    assert order.order_id not in mgr.active_orders
