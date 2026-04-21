import datetime
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from core.order_management.order_manager import OrderManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPTIONS_ROOT = PROJECT_ROOT / "strategies" / "options"
OPTIONS_SRC = OPTIONS_ROOT / "src"
for path in (str(OPTIONS_ROOT), str(OPTIONS_SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

from strategies.options import live_options_squeeze_monitor as options_module
from strategies.options.theta_gang import SpreadLeg, ThetaGangManager


class RecoveryComboBroker:
    def __init__(self, *, combo_trades=None, open_orders=None, filled_trades=None):
        self.combo_trades = list(combo_trades or [])
        self.open_orders = list(open_orders or [])
        self.filled_trades = list(filled_trades or [])
        self.calls = []
        self.place_comboorder = MagicMock()

    def describe_trade(self, trade):
        return {"ordno": getattr(trade, "ordno", None)}

    def update_combostatus(self, account=None):
        self.calls.append(("update_combostatus", account))
        return None

    def list_combotrades(self):
        self.calls.append(("list_combotrades", None))
        return list(self.combo_trades)

    def list_open_orders(self, account=None):
        self.calls.append(("list_open_orders", account))
        return list(self.open_orders)

    def list_trades(self, account=None):
        self.calls.append(("list_trades", account))
        return list(self.filled_trades)


def _make_contract(side: str, strike: float, code: str):
    option_right = "Call" if side == "C" else "Put"
    return SimpleNamespace(
        code=code,
        symbol=code,
        name=code,
        category="TXO",
        currency="TWD",
        delivery_month="202605",
        delivery_date="2026/05/20",
        strike_price=strike,
        option_right=option_right,
        security_type="OPT",
        exchange="TAIFEX",
        underlying_kind="I",
        underlying_code="TXO",
        unit=1,
        multiplier=50,
        limit_up=999.0,
        limit_down=0.1,
        reference=10.0,
        update_date="2026/04/21",
    )


def _make_combo_trade(*, status, action="Sell", quantity=1, price=55.0, ordno="COMBO-001", seqno="SEQ-001", deal_suffix="A"):
    leg_action = "SELL" if str(action).lower() == "sell" else "BUY"
    reverse_action = "BUY" if leg_action == "SELL" else "SELL"
    return SimpleNamespace(
        id=f"BROKER-{ordno}",
        seqno=seqno,
        ordno=ordno,
        action=action,
        quantity=quantity,
        price=price,
        status=SimpleNamespace(
            status=status,
            quantity=quantity,
            price=price,
            deals={
                "TXO22800P": [
                    {"seq": f"{deal_suffix}-LEG1", "ordno": ordno, "quantity": quantity, "price": price, "action": leg_action}
                ],
                "TXO22600P": [
                    {"seq": f"{deal_suffix}-LEG2", "ordno": ordno, "quantity": quantity, "price": price, "action": reverse_action}
                ],
            },
        ),
        contract=SimpleNamespace(code="TXO-COMBO"),
    )


def _build_live_theta_monitor(*, combo_trades=None):
    monitor = options_module.ShioajiOptionsSmartMonitor.__new__(options_module.ShioajiOptionsSmartMonitor)
    monitor.mode = "live"
    monitor.live_trading = True
    monitor.dry_run_live_orders = False
    monitor.order_mgr = OrderManager(mode="live")
    monitor.base_lots = 1
    monitor.paper_lots = 1
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
    monitor.log_trade_events = []
    monitor.log_trade = lambda *args, **kwargs: monitor.log_trade_events.append((args, kwargs))
    monitor._audit_signal = lambda *args, **kwargs: None
    monitor.sync_contract_quotes = lambda: None
    monitor._save_orders_file_wrapper = MagicMock()
    monitor._recover_orders_from_ledger = MagicMock()
    monitor._record_paper_order = MagicMock()
    monitor.api = SimpleNamespace(
        futopt_account="ACC",
        margin=lambda account: SimpleNamespace(equity=30000, order_margin_premium=50),
    )
    monitor.broker = RecoveryComboBroker(combo_trades=combo_trades or [])
    monitor.active_contracts = {
        "P": _make_contract("P", 22800, "TXO22800P"),
        "C": _make_contract("C", 23200, "TXO23200C"),
    }
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
    return monitor


def test_live_combo_recovery_prefers_broker_over_ledger():
    combo_trade = _make_combo_trade(status="Filled", ordno="COMBO-RECOV-001", price=48.0)
    monitor = _build_live_theta_monitor(combo_trades=[combo_trade])

    recovered = monitor._startup_recover_live_order_state()

    assert recovered["filled"] == 1
    assert monitor._recover_orders_from_ledger.call_count == 0
    assert monitor.broker.calls[:2] == [
        ("update_combostatus", "ACC"),
        ("list_combotrades", None),
    ]
    assert len(monitor.order_mgr.get_completed()) == 1
    recovered_order = monitor.order_mgr.get_completed()[0]
    assert recovered_order.symbol == "TXO-COMBO"
    assert recovered_order.truth_source == "broker_combo"
    assert recovered_order.raw_events[-1]["source"] == "combo_startup"


def test_combo_restart_recovery_restores_pending_order_without_resubmit():
    combo_trade = _make_combo_trade(status="Submitted", ordno="COMBO-PENDING-001", price=52.0)
    monitor = _build_live_theta_monitor(combo_trades=[combo_trade])

    recovered = monitor._startup_recover_live_order_state()

    assert recovered["open"] == 1
    assert monitor.broker.place_comboorder.call_count == 0
    assert monitor.pending_theta_combo is not None
    assert monitor.pending_theta_combo["phase"] == "entry"
    assert monitor.pending_theta_combo["order_id"] == monitor.order_mgr.get_pending()[0].order_id
