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
    monitor.full_cfg = {}  # [GSD Fix] Add missing attribute for test compatibility
    monitor.hard_stop_pct = 0.05
    monitor.base_lots = 1
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
    # 2026-06-23 Gemini CLI: Initialize _exit_in_progress and _pending_exit_request to prevent AttributeError
    monitor._exit_in_progress = False
    monitor._pending_exit_request = None
    monitor.latest_score = 1.2
    monitor.latest_iv = 0.25
    monitor.latest_mid_trend = "UP"
    monitor._entry_features = {}
    monitor._seen_fill_ordnos = set()
    monitor._seen_fill_identities = set()
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
    # 2026-06-23 Gemini CLI: Add close/mid keys for test validation compatibility
    monitor.current_option_quote = lambda side: {"bid": 9.0, "ask": 10.0, "mid": 9.5, "close": 9.0}
    monitor.spread_is_tradeable = lambda side: True
    monitor._paper_margin_check = lambda price, lots=None: True
    monitor._current_strategy_time = lambda: datetime.datetime(2026, 4, 20, 21, 0, 0)
    monitor.log_trade_events = []
    monitor.log_trade = lambda *args, **kwargs: monitor.log_trade_events.append((args, kwargs))
    monitor._save_orders_file_wrapper = lambda: None
    monitor._audit_signal = lambda *args, **kwargs: None
    monitor.sync_contract_quotes = lambda: None
    monitor.status_mode_label = lambda: "PAPER"
    monitor.live_trading = False
    monitor.dry_run_live_orders = False
    monitor.api = None
    monitor.broker = None
    monitor.m_cfg = {"tp1_pct": 0.05}
    monitor.trailing_stop_pct = 0.0
    monitor.last_signal = None
    monitor.entry_score = 1.0
    monitor.opening_grace_mins = 0
    monitor.score_floor = 0.0
    monitor.max_holding_days = 5
    monitor._update_theta_release_confirmation = lambda signal, spot: {"confirmed": True, "reason": "test"}
    return monitor


def _build_live_combo_monitor():
    monitor = _build_options_monitor()
    monitor.mode = "live"
    monitor.live_trading = True
    monitor.order_mgr = OrderManager(mode="live")
    monitor.pending_theta_combo = None
    monitor._theta_cfg = {"enabled": True}
    monitor._theta_bars_held = 0
    monitor._theta_release_confirm_count = 0
    monitor._theta_release_last_bar_ts = None
    monitor.api = SimpleNamespace(
        futopt_account="ACC",
        margin=lambda account: SimpleNamespace(equity=30000, order_margin_premium=50),
    )
    monitor._theta_gang = SimpleNamespace(position=None, strategy="bull_put_spread")
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


def test_options_duplicate_entry_fill_is_ignored():
    monitor = _build_options_monitor()
    monitor.dry_run_live_orders = True
    order = monitor.order_mgr.create_order("TXO-C", OrderSide.BUY, OrderType.MARKET, 1)
    monitor.order_mgr.attach_submission(order.order_id, broker_order_id="BROKER-004", ordno="ORDNO-004", raw_status="Submitted")
    monitor.pending_entry = {
        "order_id": order.order_id,
        "side": "C",
        "contract_code": "TXO-C",
        "entry_mtx_price": 23000.0,
        "signal_time": datetime.datetime(2026, 4, 20, 21, 10),
    }

    fill_msg = {
        "action": "Buy",
        "price": 10.0,
        "quantity": 1,
        "ordno": "ORDNO-004",
        "code": "TXO-C",
        "trade_id": "TRADE-004",
        "exchange_seq": "XS-004",
    }
    monitor.on_order_event("MOCK_FILL", fill_msg)
    monitor.on_order_event("MOCK_FILL", fill_msg)

    assert monitor.position == 1
    assert len(order.fills) == 1


def test_options_duplicate_exit_fill_is_ignored():
    monitor = _build_options_monitor()
    monitor.dry_run_live_orders = True
    monitor.live_trading = True
    monitor.position = 1
    monitor.active_side = "C"
    monitor.entry_price = 9.5
    order = monitor.order_mgr.create_order("TXO-C", OrderSide.SELL, OrderType.MARKET, 1)
    monitor.order_mgr.attach_submission(order.order_id, broker_order_id="BROKER-005", ordno="ORDNO-005", raw_status="Submitted")
    monitor.pending_exit_qty = 1
    monitor.pending_exit_reason = "LIVE_EXIT_SUBMITTED"
    monitor.pending_exit_trade = {
        "order_id": order.order_id,
        "trade": SimpleNamespace(),
        "quantity": 1,
    }

    fill_msg = {
        "action": "Sell",
        "price": 10.0,
        "quantity": 1,
        "ordno": "ORDNO-005",
        "code": "TXO-C",
        "trade_id": "TRADE-005",
        "exchange_seq": "XS-005",
    }
    monitor.on_order_event("MOCK_FILL", fill_msg)
    monitor.on_order_event("MOCK_FILL", fill_msg)

    assert monitor.position == 0
    assert len(order.fills) == 1
    assert monitor.pending_exit_trade is None


def test_resolve_entry_lots_uses_base_lots_without_mutating_runtime_default():
    monitor = _build_options_monitor()
    monitor.position = 0

    first_lots = monitor._resolve_entry_lots(1.8)
    second_lots = monitor._resolve_entry_lots(1.8)

    assert first_lots == 2
    assert second_lots == 2
    assert monitor.base_lots == 1
    assert monitor.paper_lots == 1


def test_live_entry_fill_clears_pending_by_requested_quantity_not_global_default():
    monitor = _build_options_monitor()
    monitor.live_trading = True
    monitor.dry_run_live_orders = True
    order = monitor.order_mgr.create_order("TXO-C", OrderSide.BUY, OrderType.MARKET, 2)
    monitor.order_mgr.attach_submission(order.order_id, broker_order_id="BROKER-004B", ordno="ORDNO-004B", raw_status="Submitted")
    monitor.pending_entry = {
        "order_id": order.order_id,
        "side": "C",
        "contract_code": "TXO-C",
        "entry_mtx_price": 23000.0,
        "signal_time": datetime.datetime(2026, 4, 20, 21, 10),
        "requested_qty": 2,
    }

    monitor.on_order_event(
        "MOCK_FILL",
        {"action": "Buy", "price": 10.0, "quantity": 2, "ordno": "ORDNO-004B", "code": "TXO-C", "trade_id": "TRADE-004B", "exchange_seq": "XS-004B"},
    )

    assert monitor.position == 2
    assert monitor.pending_entry is None


def test_recover_live_orders_from_broker_populates_order_manager():
    monitor = _build_options_monitor()
    monitor.live_trading = True
    monitor.order_mgr = OrderManager(mode="live")
    monitor.api = SimpleNamespace(futopt_account="ACC")
    monitor.broker = SimpleNamespace(
        list_open_orders=lambda account=None: [
            SimpleNamespace(ordno="OPEN-001", exchange_order_id="OPEN-001", symbol="TXO-C", action="Buy", quantity=1, price=10.0)
        ],
        list_trades=lambda account=None: [
            SimpleNamespace(ordno="FILL-001", exchange_order_id="FILL-001", symbol="TXO-P", action="Sell", quantity=1, price=12.0)
        ],
    )

    recovered = monitor._recover_live_orders_from_broker()

    assert recovered == {"filled": 1, "open": 1, "failed": 0}
    assert len(monitor.order_mgr.get_pending()) == 1
    assert len(monitor.order_mgr.get_completed()) == 1


def test_combo_partial_fill_updates_existing_lifecycle_without_opening_theta_state():
    from strategies.options.theta_gang import SpreadLeg, ThetaGangManager

    monitor = options_module.ShioajiOptionsSmartMonitor.__new__(options_module.ShioajiOptionsSmartMonitor)
    monitor.mode = "live"
    monitor.live_trading = True
    monitor.order_mgr = OrderManager(mode="live")
    monitor.position = 0
    monitor.active_side = None
    monitor.entry_price = 0.0
    monitor.entry_time = None
    monitor.entry_mtx_price = 0.0
    monitor.stop_loss_pct = 0.1
    monitor.stop_loss_price = 0.0
    monitor.peak_premium = 0.0
    monitor.has_tp1_hit = False
    monitor.pending_entry = None
    monitor.pending_exit_qty = 0
    monitor.pending_exit_reason = None
    monitor.pending_exit_trade = None
    monitor.pending_theta_combo = None
    monitor.cooldown_bars = 0
    monitor.cooldown_until = 0
    monitor._theta_cfg = {"enabled": True}
    monitor._theta_bars_held = 0
    monitor._theta_release_confirm_count = 0
    monitor._theta_release_last_bar_ts = None
    monitor._seen_fill_ordnos = set()
    monitor._seen_fill_identities = set()
    monitor.sync_contract_quotes = lambda: None
    monitor._audit_signal = lambda *args, **kwargs: None
    monitor.log_trade_events = []
    monitor.log_trade = lambda *args, **kwargs: monitor.log_trade_events.append((args, kwargs))
    monitor._save_orders_file_wrapper = lambda: None
    monitor.api = SimpleNamespace(
        futopt_account="ACC",
        margin=lambda account: SimpleNamespace(equity=30000, order_margin_premium=50),
    )
    theta_cfg = {
        "theta_gang": {
            "strategy": "bull_put_spread",
            "wing_width": 200,
            "otm_offset": 200,
            "quantity": 2,
            "min_iv": 0.18,
            "min_credit": 10,
            "take_profit_pct": 0.50,
            "max_loss_pct": 1.0,
            "min_dte_entry": 5,
            "min_dte_exit": 3,
            "exit_on_squeeze_release": True,
            "risk_free_rate": 0.02,
        }
    }
    monitor._theta_gang = ThetaGangManager(theta_cfg, lambda *args, **kwargs: {"price": 10.0}, 100)
    monitor.active_contracts = {
        "P": SimpleNamespace(code="TXO22800P", delivery_date="2026/05/20"),
        "C": SimpleNamespace(code="TXO23200C", delivery_date="2026/05/20"),
    }
    monitor.broker = SimpleNamespace(
        place_comboorder=lambda *args, **kwargs: SimpleNamespace(id="BROKER-COMBO-002", seqno="SEQ-COMBO-002", ordno="ORDNO-COMBO-002"),
        describe_trade=lambda trade: {"ordno": trade.ordno},
        update_combostatus=lambda account=None: None,
        list_combotrades=lambda: [
            SimpleNamespace(
                id="BROKER-COMBO-002",
                seqno="SEQ-COMBO-002",
                ordno="ORDNO-COMBO-002",
                action="Sell",
                quantity=2,
                price=50.0,
                status=SimpleNamespace(
                    status="PartFilled",
                    quantity=2,
                    price=50.0,
                    deals={
                        "TXO22800P": [{"seq": "LEG1-A", "ordno": "ORDNO-COMBO-002", "quantity": 1, "price": 50.0}],
                        "TXO22600P": [{"seq": "LEG2-A", "ordno": "ORDNO-COMBO-002", "quantity": 1, "price": 50.0}],
                    },
                ),
            )
        ],
    )

    submitted = monitor._submit_live_theta_combo_entry(
        {
            "strategy": "bull_put_spread",
            "legs": [
                SpreadLeg("P", 22800, "SELL", premium=60.0, contract=SimpleNamespace(code="TXO22800P", strike_price=22800, option_right="Put", delivery_date="2026/05/20")),
                SpreadLeg("P", 22600, "BUY", premium=10.0, contract=SimpleNamespace(code="TXO22600P", strike_price=22600, option_right="Put", delivery_date="2026/05/20")),
            ],
            "net_credit": 50.0,
            "max_loss": 150.0,
            "quantity": 2,
        }
    )

    assert submitted is True

    reconciled = monitor._reconcile_theta_combo_orders(source="combo_poll", reason="runtime_poll")
    order = monitor.order_mgr.get_order(monitor.pending_theta_combo["order_id"])

    assert reconciled["fills_applied"] == 1
    assert order.status == OrderStatus.PARTIAL_FILLED
    assert order.filled_quantity == 1
    assert monitor.position == 0
    assert monitor.active_side is None
    assert monitor._theta_gang.position is None
    assert monitor.pending_theta_combo is not None


def test_combo_cancel_and_reject_preserve_audit_without_fabricated_fill():
    order_mgr = OrderManager(mode="paper")
    order = order_mgr.create_order("TXO-COMBO", OrderSide.SELL, OrderType.LIMIT, 1, price=42.0, truth_source="broker_combo")
    order_mgr.attach_submission(order.order_id, broker_order_id="BROKER-COMBO-003", ordno="ORDNO-COMBO-003", raw_status="Submitted")

    cancelled = order_mgr.reconcile_combo_trade_snapshot(
        order.order_id,
        combo_trade=SimpleNamespace(
            id="BROKER-COMBO-003",
            seqno="SEQ-COMBO-003",
            ordno="ORDNO-COMBO-003",
            action="Sell",
            quantity=1,
            price=42.0,
            status=SimpleNamespace(status="Cancelled", deals={}),
        ),
        source="combo_poll",
        reason="cancelled_by_broker",
    )

    assert cancelled["matched"] is True
    assert order.status == OrderStatus.CANCELLED
    assert order.fills == []
    assert order.raw_events[-1]["source"] == "combo_poll"
    assert order.raw_events[-1]["reason"] == "cancelled_by_broker"

    rejected_order = order_mgr.create_order("TXO-COMBO", OrderSide.SELL, OrderType.LIMIT, 1, price=42.0, truth_source="broker_combo")
    order_mgr.attach_submission(rejected_order.order_id, broker_order_id="BROKER-COMBO-004", ordno="ORDNO-COMBO-004", raw_status="Submitted")
    rejected = order_mgr.reconcile_combo_trade_snapshot(
        rejected_order.order_id,
        combo_trade=SimpleNamespace(
            id="BROKER-COMBO-004",
            seqno="SEQ-COMBO-004",
            ordno="ORDNO-COMBO-004",
            action="Sell",
            quantity=1,
            price=42.0,
            status=SimpleNamespace(status="Failed", deals={}),
        ),
        source="combo_restart",
        reason="broker_restart_reject",
    )

    assert rejected["matched"] is True
    assert rejected_order.status == OrderStatus.REJECTED
    assert rejected_order.fills == []
    assert rejected_order.raw_events[-1]["source"] == "combo_restart"
    assert rejected_order.raw_events[-1]["reason"] == "broker_restart_reject"


def test_combo_recovered_fill_is_applied_once_without_resubmit():
    from strategies.options.theta_gang import SpreadLeg, SpreadPosition, ThetaGangManager

    monitor = options_module.ShioajiOptionsSmartMonitor.__new__(options_module.ShioajiOptionsSmartMonitor)
    monitor.mode = "live"
    monitor.live_trading = True
    monitor.order_mgr = OrderManager(mode="live")
    monitor.position = 1
    monitor.active_side = "THETA"
    monitor.entry_price = 55.0
    monitor.entry_time = datetime.datetime(2026, 4, 20, 21, 0, 0)
    monitor.stop_loss_pct = 0.1
    monitor.stop_loss_price = 60.5
    monitor.peak_premium = 55.0
    monitor.has_tp1_hit = False
    monitor.pending_entry = None
    monitor.pending_exit_qty = 0
    monitor.pending_exit_reason = None
    monitor.pending_exit_trade = None
    monitor.pending_theta_combo = None
    monitor.cooldown_bars = 0
    monitor.cooldown_until = 0
    monitor._theta_cfg = {"enabled": True}
    monitor._theta_bars_held = 2
    monitor._theta_release_confirm_count = 0
    monitor._theta_release_last_bar_ts = None
    monitor._seen_fill_ordnos = set()
    monitor._seen_fill_identities = set()
    monitor.sync_contract_quotes = lambda: None
    monitor._audit_signal = lambda *args, **kwargs: None
    monitor.log_trade_events = []
    monitor.log_trade = lambda *args, **kwargs: monitor.log_trade_events.append((args, kwargs))
    monitor._save_orders_file_wrapper = lambda: None
    monitor.api = SimpleNamespace(
        futopt_account="ACC",
        margin=lambda account: SimpleNamespace(equity=30000, order_margin_premium=50),
    )
    theta_cfg = {
        "theta_gang": {
            "strategy": "bull_put_spread",
            "wing_width": 200,
            "otm_offset": 200,
            "quantity": 1,
            "min_iv": 0.18,
            "min_credit": 10,
            "take_profit_pct": 0.50,
            "max_loss_pct": 1.0,
            "min_dte_entry": 5,
            "min_dte_exit": 3,
            "exit_on_squeeze_release": True,
            "risk_free_rate": 0.02,
        }
    }
    monitor._theta_gang = ThetaGangManager(theta_cfg, lambda *args, **kwargs: {"price": 10.0}, 100)
    monitor._theta_gang.position = SpreadPosition(
        strategy="bull_put_spread",
        legs=[SpreadLeg("P", 22800, "SELL"), SpreadLeg("P", 22600, "BUY")],
        entry_time=datetime.datetime(2026, 4, 20, 21, 0, 0),
        net_credit=55.0,
        max_loss=145.0,
        quantity=1,
    )
    monitor.active_contracts = {
        "P": SimpleNamespace(code="TXO22800P", delivery_date="2026/05/20"),
        "C": SimpleNamespace(code="TXO23200C", delivery_date="2026/05/20"),
    }
    monitor.broker = SimpleNamespace(
        place_comboorder=MagicMock(),
        describe_trade=lambda trade: {"ordno": trade.ordno},
        update_combostatus=lambda account=None: None,
        list_combotrades=lambda: [
            SimpleNamespace(
                id="BROKER-COMBO-EXIT-001",
                seqno="SEQ-COMBO-EXIT-001",
                ordno="ORDNO-COMBO-EXIT-001",
                action="Buy",
                quantity=1,
                price=20.0,
                status=SimpleNamespace(
                    status="Filled",
                    quantity=1,
                    price=20.0,
                    deals={
                        "TXO22800P": [{"seq": "EXIT-LEG1", "ordno": "ORDNO-COMBO-EXIT-001", "quantity": 1, "price": 20.0}],
                        "TXO22600P": [{"seq": "EXIT-LEG2", "ordno": "ORDNO-COMBO-EXIT-001", "quantity": 1, "price": 20.0}],
                    },
                ),
            )
        ],
    )

    exit_order = monitor.order_mgr.create_order(
        symbol="TXO-COMBO",
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=1,
        price=20.0,
        truth_source="broker_combo",
        combo_strategy="bull_put_spread",
        combo_legs=[
            {"code": "TXO22800P", "action": "BUY", "side": "P", "strike": 22800},
            {"code": "TXO22600P", "action": "SELL", "side": "P", "strike": 22600},
        ],
    )
    monitor.order_mgr.attach_submission(
        exit_order.order_id,
        broker_order_id="BROKER-COMBO-EXIT-001",
        seqno="SEQ-COMBO-EXIT-001",
        ordno="ORDNO-COMBO-EXIT-001",
        raw_status="Submitted",
        source="broker_combo_submit",
    )
    monitor.pending_theta_combo = {"phase": "exit", "order_id": exit_order.order_id, "strategy": "bull_put_spread", "requested_qty": 1}

    first = monitor._reconcile_theta_combo_orders(source="combo_restart", reason="startup_recovery")
    second = monitor._reconcile_theta_combo_orders(source="combo_restart", reason="startup_recovery")

    assert first["fills_applied"] == 1
    assert second["fills_applied"] == 0
    assert len(exit_order.fills) == 1
    assert monitor.position == 0
    assert monitor.active_side is None
    assert monitor.pending_theta_combo is None
    assert monitor._theta_gang.position is None
    assert monitor.broker.place_comboorder.call_count == 0


def test_combo_startup_partial_recovery_stays_pending_without_theta_fill():
    monitor = _build_live_combo_monitor()
    monitor.broker = SimpleNamespace(
        list_combo_status_trades=lambda account=None: [
            SimpleNamespace(
                id="BROKER-COMBO-STARTUP-002",
                seqno="SEQ-COMBO-STARTUP-002",
                ordno="ORDNO-COMBO-STARTUP-002",
                action="Sell",
                quantity=2,
                strategy="bull_put_spread",
                price=50.0,
                status=SimpleNamespace(
                    status="PartFilled",
                    quantity=2,
                    price=50.0,
                    deals={
                        "TXO22800P": [{"seq": "LEG1-START", "ordno": "ORDNO-COMBO-STARTUP-002", "quantity": 1, "price": 50.0}],
                        "TXO22600P": [{"seq": "LEG2-START", "ordno": "ORDNO-COMBO-STARTUP-002", "quantity": 1, "price": 50.0}],
                    },
                ),
            )
        ],
        list_open_orders=lambda account=None: [],
        list_trades=lambda account=None: [],
    )

    recovered = monitor._recover_live_orders_from_broker()
    order = monitor.order_mgr.get_pending()[0]

    assert recovered == {"filled": 0, "open": 1, "failed": 0}
    assert order.status == OrderStatus.PARTIAL_FILLED
    assert order.truth_source == "broker_combo"
    assert order.filled_quantity == 1
    assert monitor.pending_theta_combo["order_id"] == order.order_id
    assert monitor.position == 0
    assert monitor._theta_gang.position is None


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
    # 2026-06-23 Gemini CLI: Add close key for test validation compatibility
    monitor.current_option_quote = lambda side: {"bid": 11.0, "ask": 12.0, "mid": 11.5, "close": 11.0}

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
    # 2026-06-23 Gemini CLI: In apply_paper_tp1, position is decremented before recording the paper order, so position is 1
    assert positions_at_record == [1]
    assert monitor.position == 1
    assert monitor.has_tp1_hit is True
    tp1_order = monitor.order_mgr.completed[-1]
    assert tp1_order.comment.startswith("PAPER_TP1")
    assert tp1_order.fills[0].deal_id == f"deal-{tp1_order.order_id}"
