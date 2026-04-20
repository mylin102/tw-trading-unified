import datetime
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock
from pathlib import Path

from core.order_management.order import OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OPTIONS_ROOT = PROJECT_ROOT / "strategies" / "options"
OPTIONS_SRC = OPTIONS_ROOT / "src"
for path in (str(OPTIONS_ROOT), str(OPTIONS_SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

from strategies.options import live_options_squeeze_monitor as options_module


def _build_options_monitor():
    monitor = options_module.ShioajiOptionsSmartMonitor.__new__(options_module.ShioajiOptionsSmartMonitor)
    monitor.order_mgr = OrderManager(mode="paper")
    monitor.mode = "paper"
    monitor.paper_lots = 1
    monitor.max_positions = 2
    monitor.min_dte_to_exit = None
    monitor.entry_premium_limit = 999.0
    monitor.position = 0
    monitor.active_side = None
    monitor.entry_price = 0.0
    monitor.entry_mtx_price = 0.0
    monitor.entry_time = None
    monitor.has_tp1_hit = False
    monitor.stop_loss_pct = 0.1
    monitor.stop_loss_price = 0.0
    monitor.peak_premium = 0.0
    monitor.cooldown_bars = 0
    monitor.cooldown_until = 0
    monitor.replay_stats = {"entries": 0, "exits": 0, "tp1_hits": 0}
    monitor.latest_score = 1.2
    monitor.latest_iv = 0.25
    monitor.latest_mid_trend = "UP"
    monitor._entry_features = {}
    monitor._seen_fill_ordnos = set()
    monitor.pending_entry = None
    monitor.pending_exit_qty = 0
    monitor.pending_exit_reason = None
    monitor.pending_exit_trade = None
    monitor.active_contracts = {
        "C": SimpleNamespace(code="TXO-C", delivery_date="2026/05/20"),
        "P": SimpleNamespace(code="TXO-P", delivery_date="2026/05/20"),
    }
    monitor.market_data = {
        "C": {"bid": 9.0, "ask": 10.0},
        "P": {"bid": 9.0, "ask": 10.0},
        "MTX": {"close": 23000.0},
    }
    monitor.current_option_quote = lambda side: {"bid": 9.0, "ask": 10.0}
    monitor.spread_is_tradeable = lambda side: True
    monitor._paper_margin_check = lambda price: True
    monitor._current_strategy_time = lambda: datetime.datetime(2026, 4, 20, 21, 0, 0)
    monitor.log_trade_events = []
    monitor.log_trade = lambda *args, **kwargs: monitor.log_trade_events.append((args, kwargs))
    monitor._save_orders_file_wrapper = lambda: None
    monitor._audit_signal = lambda *args, **kwargs: None
    monitor.sync_contract_quotes = lambda: None
    monitor.status_mode_label = lambda: "PAPER"
    monitor.live_trading = False
    monitor.m_cfg = {"tp1_pct": 0.05}
    monitor.trailing_stop_pct = 0.0
    monitor.last_signal = None
    monitor.entry_score = 1.0
    monitor.opening_grace_mins = 0
    monitor.score_floor = 0.0
    monitor.max_holding_days = 5
    monitor._update_theta_release_confirmation = lambda signal, spot: {"confirmed": True, "reason": "test"}
    return monitor


def test_order_update_does_not_create_fill():
    order_mgr = OrderManager(mode="paper")
    order = order_mgr.create_order("TXO", OrderSide.BUY, OrderType.MARKET, 1)
    order_mgr.attach_submission(order.order_id, broker_order_id="BROKER-001", ordno="ORDNO-001", raw_status="Submitted")
    order_mgr.apply_order_update(
        order.order_id,
        raw_status="Submitted",
        broker_order_id="BROKER-001",
        ordno="ORDNO-001",
        raw_payload={"status": "Submitted"},
    )

    assert order.status == OrderStatus.SUBMITTED
    assert order.fills == []


def test_cancel_after_partial_fill_keeps_fill_history():
    order_mgr = OrderManager(mode="paper")
    order = order_mgr.create_order("TXO", OrderSide.BUY, OrderType.MARKET, 2)
    order_mgr.attach_submission(order.order_id, broker_order_id="BROKER-002", ordno="ORDNO-002", raw_status="Submitted")
    order_mgr.apply_deal_fill(
        order.order_id,
        deal_id="deal-001",
        fill_price=10.0,
        fill_qty=1,
    )
    order_mgr.apply_order_update(order.order_id, raw_status="Cancelled", reason="user_cancel")

    assert order.status == OrderStatus.CANCELLED
    assert order.filled_quantity == 1
    assert len(order.fills) == 1
    assert order.fills[0].deal_id == "deal-001"


def test_options_paper_entry_records_mock_deal_before_state_persists():
    monitor = _build_options_monitor()

    monitor.enter_paper_position("C", {"side": "C", "score": 1.5, "timestamp": datetime.datetime(2026, 4, 20, 21, 5), "price_mtx": 23010})

    assert monitor.position == 1
    assert monitor.active_side == "C"
    assert len(monitor.order_mgr.completed) == 1
    paper_order = monitor.order_mgr.completed[0]
    assert paper_order.intent_id.startswith("intent_")
    assert paper_order.fills[0].deal_id == f"deal-{paper_order.order_id}"


def test_options_callbacks_preserve_intent_order_and_deal_linkage():
    monitor = _build_options_monitor()
    monitor.dry_run_live_orders = True
    order = monitor.order_mgr.create_order("TXO-C", OrderSide.BUY, OrderType.MARKET, 1)
    monitor.order_mgr.attach_submission(order.order_id, broker_order_id="BROKER-003", ordno="ORDNO-003", raw_status="Submitted")
    monitor.pending_entry = {
        "order_id": order.order_id,
        "side": "C",
        "contract_code": "TXO-C",
        "entry_mtx_price": 23000.0,
        "signal_time": datetime.datetime(2026, 4, 20, 21, 10),
    }
    pending_order_id = order.order_id

    monitor.on_order_event(
        options_module.sj.constant.OrderState.StockOrder,
        {"action": "Buy", "status": "Submitted", "ordno": "ORDNO-003", "code": "TXO-C"},
    )
    monitor.on_order_event(
        "MOCK_FILL",
        {"action": "Buy", "price": 10.0, "quantity": 1, "ordno": "ORDNO-003", "code": "TXO-C", "trade_id": "TRADE-003", "exchange_seq": "XS-003"},
    )

    assert order.intent_id.startswith("intent_")
    assert order.order_id == pending_order_id
    assert order.fills[0].deal_id.startswith("deal_")
    assert order.fills[0].broker_trade_id == "TRADE-003"
    assert monitor.position == 1


def test_options_paper_exit_records_mock_deal_before_position_zero():
    monitor = _build_options_monitor()
    monitor.enter_paper_position("C", {"side": "C", "score": 1.5, "timestamp": datetime.datetime(2026, 4, 20, 21, 5), "price_mtx": 23010})

    positions_at_record = []
    original_record = monitor._record_paper_order

    def _wrapped_record(*args, **kwargs):
        positions_at_record.append(monitor.position)
        return original_record(*args, **kwargs)

    monitor._record_paper_order = _wrapped_record
    monitor.exit_paper_position("PAPER_EXIT", 11.0, "reason=test")

    assert positions_at_record == [1]
    assert monitor.position == 0
    assert len(monitor.order_mgr.completed) == 2
    exit_order = monitor.order_mgr.completed[-1]
    assert exit_order.fills[0].deal_id == f"deal-{exit_order.order_id}"


def test_options_paper_tp1_records_partial_exit_lifecycle_before_position_changes():
    monitor = _build_options_monitor()
    monitor.enter_paper_position("C", {"side": "C", "score": 1.5, "timestamp": datetime.datetime(2026, 4, 20, 21, 5), "price_mtx": 23010})
    monitor.position = 2
    monitor.has_tp1_hit = False
    monitor.current_option_quote = lambda side: {"bid": 11.0, "ask": 12.0}

    positions_at_record = []
    original_record = monitor._record_paper_order

    def _wrapped_record(*args, **kwargs):
        positions_at_record.append(monitor.position)
        return original_record(*args, **kwargs)

    monitor._record_paper_order = _wrapped_record
    handled = monitor.manage_open_position({
        "timestamp": datetime.datetime(2026, 4, 20, 21, 15),
        "score": 0.0,
    })

    assert handled is False
    assert positions_at_record == [2]
    assert monitor.position == 1
    assert monitor.has_tp1_hit is True
    tp1_order = monitor.order_mgr.completed[-1]
    assert tp1_order.comment.startswith("PAPER_TP1")
    assert tp1_order.fills[0].deal_id == f"deal-{tp1_order.order_id}"
