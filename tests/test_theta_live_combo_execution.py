import datetime
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.order_management.order_manager import OrderManager

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OPTIONS_ROOT = PROJECT_ROOT / "strategies" / "options"
OPTIONS_SRC = OPTIONS_ROOT / "src"
for path in (str(OPTIONS_ROOT), str(OPTIONS_SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

from strategies.options import live_options_squeeze_monitor as options_module
from strategies.options.theta_gang import SpreadLeg, SpreadPosition, ThetaGangManager


class FakeComboBroker:
    def __init__(self):
        self.combo_calls = []

    def place_comboorder(self, legs, price, quantity, action=None, order_type=None, octype=None, price_type="LMT"):
        self.combo_calls.append(
            {
                "legs": legs,
                "price": price,
                "quantity": quantity,
                "action": action,
                "order_type": order_type,
                "octype": octype,
                "price_type": price_type,
            }
        )
        return SimpleNamespace(id="BROKER-COMBO-001", seqno="SEQ-COMBO-001", ordno="ORDNO-COMBO-001")

    def describe_trade(self, trade):
        return {"id": trade.id, "ordno": trade.ordno}


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


def _build_live_theta_monitor(strategy="bull_put_spread", *, equity=20000):
    monitor = options_module.ShioajiOptionsSmartMonitor.__new__(options_module.ShioajiOptionsSmartMonitor)
    monitor.mode = "live"
    monitor.live_trading = True
    monitor.dry_run_live_orders = False
    monitor.order_mgr = OrderManager(mode="paper")
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
    monitor._theta_cfg = {"enabled": True}
    monitor._theta_live_audit = []
    monitor._audit_signal = lambda *args: monitor._theta_live_audit.append(args)
    monitor.log_trade_events = []
    monitor.log_trade = lambda *args, **kwargs: monitor.log_trade_events.append((args, kwargs))
    monitor.sync_contract_quotes = lambda: None
    monitor._save_orders_file_wrapper = lambda: None
    monitor._record_paper_order = MagicMock()
    monitor.broker = FakeComboBroker()
    monitor.api = SimpleNamespace(
        futopt_account="ACC",
        margin=lambda account: SimpleNamespace(equity=equity, order_margin_premium=50),
    )
    theta_cfg = {
        "theta_gang": {
            "strategy": strategy,
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


def test_live_vertical_theta_uses_combo_order():
    monitor = _build_live_theta_monitor("bull_put_spread")
    entry_info = {
        "strategy": "bull_put_spread",
        "legs": [
            SpreadLeg("P", 22800, "SELL", premium=60.0, contract=_make_contract("P", 22800, "TXO22800P")),
            SpreadLeg("P", 22600, "BUY", premium=5.0, contract=_make_contract("P", 22600, "TXO22600P")),
        ],
        "net_credit": 55.0,
        "max_loss": 145.0,
        "quantity": 1,
    }

    submitted = monitor._submit_live_theta_combo_entry(entry_info)

    assert submitted is True
    assert len(monitor.broker.combo_calls) == 1
    assert monitor._record_paper_order.call_count == 0
    assert monitor.pending_theta_combo["phase"] == "entry"
    assert monitor.pending_theta_combo["strategy"] == "bull_put_spread"
    assert monitor.position == 0
    assert monitor.active_side is None
    assert monitor._theta_gang.position is None
    active_orders = list(monitor.order_mgr.active_orders.values())
    assert len(active_orders) == 1
    assert active_orders[0].symbol == "TXO-COMBO"
    assert active_orders[0].truth_source == "broker_combo"
    assert active_orders[0].combo_strategy == "bull_put_spread"


def test_live_bear_call_theta_exit_uses_reversed_combo_and_keeps_state_pending():
    monitor = _build_live_theta_monitor("bear_call_spread")
    open_position = SpreadPosition(
        strategy="bear_call_spread",
        legs=[
            SpreadLeg("C", 23200, "SELL", premium=48.0, contract=_make_contract("C", 23200, "TXO23200C")),
            SpreadLeg("C", 23400, "BUY", premium=8.0, contract=_make_contract("C", 23400, "TXO23400C")),
        ],
        entry_time=datetime.datetime(2026, 4, 21, 9, 5, 0),
        net_credit=40.0,
        max_loss=160.0,
        quantity=1,
    )
    monitor._theta_gang.position = open_position
    monitor.position = 1
    monitor.active_side = "THETA"
    monitor.entry_price = 40.0

    submitted = monitor._submit_live_theta_combo_exit(
        {"reason": "TP 50% >= 50%", "current_value": 18.0, "pnl": 900}
    )

    assert submitted is True
    assert len(monitor.broker.combo_calls) == 1
    assert [leg["action"] for leg in monitor.broker.combo_calls[0]["legs"]] == ["BUY", "SELL"]
    assert monitor._record_paper_order.call_count == 0
    assert monitor.pending_theta_combo["phase"] == "exit"
    assert monitor.position == 1
    assert monitor.active_side == "THETA"
    assert monitor._theta_gang.position is open_position
    active_orders = list(monitor.order_mgr.active_orders.values())
    assert len(active_orders) == 1
    assert active_orders[0].combo_strategy == "bear_call_spread"


@pytest.mark.parametrize("strategy,leg_count", [("iron_condor", 4), ("short_strangle", 2)])
def test_live_theta_unsupported_strategies_are_blocked_without_broker_or_paper(strategy, leg_count):
    monitor = _build_live_theta_monitor(strategy)
    legs = [
        SpreadLeg("P" if index < leg_count // 2 else "C", 22000 + (index * 100), "SELL" if index % 2 == 0 else "BUY")
        for index in range(leg_count)
    ]
    entry_info = {"strategy": strategy, "legs": legs, "net_credit": 60.0, "max_loss": 140.0, "quantity": 1}

    with patch.object(options_module.console, "print") as print_mock:
        submitted = monitor._submit_live_theta_combo_entry(entry_info)

    assert submitted is False
    assert len(monitor.broker.combo_calls) == 0
    assert monitor._record_paper_order.call_count == 0
    assert monitor.pending_theta_combo is None
    assert len(monitor.order_mgr.active_orders) == 0
    assert monitor._theta_live_audit[-1][-1] == f"unsupported_live_strategy:{strategy}"
    assert strategy in print_mock.call_args[0][0]


def test_combo_risk_precheck_uses_spread_max_loss_not_single_leg_premium():
    monitor = _build_live_theta_monitor("bull_put_spread", equity=8000)
    entry_info = {
        "strategy": "bull_put_spread",
        "legs": [
            SpreadLeg("P", 22800, "SELL", premium=60.0),
            SpreadLeg("P", 22600, "BUY", premium=5.0),
        ],
        "net_credit": 60.0,
        "max_loss": 0.0,
        "quantity": 1,
    }

    assert monitor._margin_sufficient(combo_entry=entry_info) is False
