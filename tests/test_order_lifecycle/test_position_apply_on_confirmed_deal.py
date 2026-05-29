from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.order_management.order_manager import OrderManager
from core.order_management.paper_fill import PaperFillSimulator
from strategies.futures.monitor import FuturesMonitor
from strategies.futures.squeeze_futures.engine.simulator import PaperTrader


def _build_monitor(*, live_trading: bool, ticker: str = "TMF"):
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.ticker = ticker
    monitor.contract = SimpleNamespace(code=ticker)
    monitor.live_trading = live_trading
    monitor.dry_run = not live_trading
    monitor.client = MagicMock()
    monitor.client.place_order.return_value = SimpleNamespace(id="TRADE-001", seqno="SEQ-001", ordno="ORD-001")
    monitor.order_mgr = OrderManager(mode="live" if live_trading else "paper")
    monitor.paper_fill_sim = None
    if not live_trading:
        monitor.paper_fill_sim = PaperFillSimulator(monitor.order_mgr)
        monitor.order_mgr.set_simulator(monitor.paper_fill_sim)
    monitor._use_order_manager = True
    monitor.trader = PaperTrader(initial_balance=100000, margin_per_lot=40000)
    monitor.MGMT = {"max_positions": 2}
    monitor.RISK = {"stop_loss_pts": 60}
    monitor._last_cross_policy = {"allow_trade": True}
    monitor._last_bar_context = {
        "momentum": 1.5,
        "mom_velo": 0.6,
        "vwap": 36440.0,
        "atr": 20.0,
        "regime": "NORMAL",
        "score": 1.5,
    }
    monitor._entry_features_futures = {}
    monitor.active_strategy_name = "counter_vwap"
    monitor._save_orders_file_wrapper = lambda: None
    monitor._cancel_safety_stop = lambda: None
    monitor._place_safety_stop = lambda *args, **kwargs: None
    monitor._pending_lifecycle_orders = {}
    monitor._applied_lifecycle_deals = set()
    monitor.consecutive_losses = 0
    monitor.session_losses = []
    monitor.session_type = "night"
    monitor._session_pnl = 0.0
    monitor._last_trade_ts = None
    monitor._bars_since_trade = 0
    monitor._signals_generated = 0
    monitor._atr_trail_peak = 0.0
    monitor._last_entry_reason = None
    monitor._wire_order_callbacks()
    return monitor


def test_live_submit_does_not_change_position():
    monitor = _build_monitor(live_trading=True)
    ts = datetime(2026, 4, 21, 9, 0)

    with patch("strategies.futures.monitor.save_trade") as save_trade, patch(
        "strategies.futures.squeeze_futures.data.data_storage.save_signal_audit"
    ) as save_signal_audit:
        order_id = monitor._execute_trade("BUY", 36450.0, ts, 1, stop_loss=60, reason="TEST_ENTRY")

    order = monitor.order_mgr.active_orders[order_id]

    assert monitor.trader.position == 0
    assert order.intent_id.startswith("intent_")
    assert monitor._pending_lifecycle_orders[order_id]["intent_id"] == order.intent_id
    assert len(order.fills) == 0
    save_trade.assert_not_called()
    save_signal_audit.assert_not_called()


def test_partial_confirmed_deal_updates_position_incrementally():
    monitor = _build_monitor(live_trading=True)
    ts = datetime(2026, 4, 21, 9, 5)

    with patch("strategies.futures.monitor.save_trade") as save_trade, patch(
        "strategies.futures.squeeze_futures.data.data_storage.save_signal_audit"
    ):
        order_id = monitor._execute_trade("BUY", 36450.0, ts, 2, stop_loss=60, reason="TEST_ENTRY")
        order = monitor.order_mgr.active_orders[order_id]

        monitor.order_mgr.apply_deal_fill(order_id, deal_id="deal-001", fill_price=36450.0, fill_qty=1, fill_time=ts)
        assert monitor.trader.position == 1
        assert order.intent_id == monitor._pending_lifecycle_orders[order_id]["intent_id"]
        assert order.fills[0].deal_id == "deal-001"
        assert save_trade.call_args_list[0].args[0]["type"] == "BUY"

        monitor.order_mgr.apply_deal_fill(order_id, deal_id="deal-002", fill_price=36455.0, fill_qty=1, fill_time=ts)

    assert monitor.trader.position == 2
    assert order.fills[-1].deal_id == "deal-002"
    assert order.order_id == order_id
