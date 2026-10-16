from types import SimpleNamespace

from core.order_management.order import Order, OrderSide, OrderStatus, OrderType
from core.order_management.paper_fill import PaperFillSimulator


class _Manager:
    def __init__(self):
        self.orders = {}
        self.fills = []
        self.rejects = []

    def on_fill(self, **payload):
        self.fills.append(payload)
        self.orders[payload["order_id"]].fill(
            payload["fill_price"], payload["fill_qty"])

    def reject(self, order_id, reason, source=""):
        self.rejects.append((order_id, reason, source))
        self.orders[order_id].status = OrderStatus.REJECTED


def _order(manager, quantity=1):
    order = Order(
        symbol="TMF1-TMF2", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=quantity, price=10.0,
        combo_legs=[
            {"symbol": "TMF1", "side": "buy", "quantity": quantity},
            {"symbol": "TMF2", "side": "sell", "quantity": quantity},
        ], combo_strategy="TIME_SPREAD")
    order.submit("PAPER-COMBO")
    manager.orders[order.order_id] = order
    return order


def _ticks(near=100.0, far=90.0):
    return (SimpleNamespace(symbol="TMF1", close=near),
            SimpleNamespace(symbol="TMF2", close=far))


def test_combo_requires_both_legs_and_fills_once():
    manager = _Manager()
    simulator = PaperFillSimulator(manager)
    order = _order(manager)
    metadata = {"spread_side": "BUY_NEAR_SELL_FAR"}
    simulator.register_combo(order, near_symbol="TMF1", far_symbol="TMF2",
                             metadata=metadata)

    near, far = _ticks()
    simulator.process_combo_ticks(near, SimpleNamespace(symbol="WRONG", close=90))
    assert manager.fills == []
    assert simulator.get_pending_count() == 1

    simulator.process_combo_ticks(near, far)
    assert order.status is OrderStatus.FILLED
    assert manager.fills[0]["fill_price"] == 10.0
    assert metadata["near_fill_price"] == 100.0
    assert metadata["far_fill_price"] == 90.0
    assert simulator.get_pending_count() == 0

    simulator.register_combo(order, near_symbol="TMF1", far_symbol="TMF2")
    assert simulator.get_pending_count() == 0


def test_combo_partial_fill_never_invents_missing_leg():
    manager = _Manager()
    simulator = PaperFillSimulator(manager)
    order = _order(manager, quantity=2)
    simulator.register_combo(order, near_symbol="TMF1", far_symbol="TMF2",
                             metadata={"spread_side": "SELL_NEAR_BUY_FAR"})
    near, far = _ticks()

    simulator.process_combo_ticks(near, far)
    assert order.status is OrderStatus.PARTIAL_FILLED
    assert order.filled_quantity == 1
    assert simulator.get_pending_count() == 1
    simulator.process_combo_ticks(near, far)
    assert order.status is OrderStatus.FILLED
    assert len(manager.fills) == 2


def test_combo_reject_consumes_parent_without_leg_orders():
    manager = _Manager()
    simulator = PaperFillSimulator(manager)
    order = _order(manager)
    simulator.register_combo(order, near_symbol="TMF1", far_symbol="TMF2")
    simulator.reject_combo(order.order_id, "PAPER_COMBO_REJECTED")

    assert manager.rejects == [
        (order.order_id, "PAPER_COMBO_REJECTED", "paper_combo")]
    assert simulator.get_pending_count() == 0
    simulator.register_combo(order, near_symbol="TMF1", far_symbol="TMF2")
    assert simulator.get_pending_count() == 0


# 2026-10-05 Codex: exercise the production gateway, reconciliation and fill
# callsites; the legacy simulator-only tests above intentionally use a double.
from datetime import datetime

import pytest

from core.order_intent_gateway import OrderIntentGateway
from core.order_management.order_manager import OrderManager
from strategies.futures.monitor import FuturesMonitor


@pytest.fixture
def real_paper_monitor(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("TRADING_FUTURES_MODE", "paper")
    monkeypatch.setenv("AGENT_NAME", "codex")
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = False
    monitor.dry_run = False
    monitor.order_mgr = OrderManager(mode="paper")
    monitor.paper_fill_sim = PaperFillSimulator(monitor.order_mgr)
    monitor.order_mgr.set_simulator(monitor.paper_fill_sim)
    monitor._order_intent_gateway = OrderIntentGateway()
    monitor._pending_lifecycle_orders = {}
    monitor._mts_pending_fills = {}
    monitor._registry = {}
    monitor.contract = SimpleNamespace(code="TMFJ6")
    monitor.far_contract = SimpleNamespace(code="TMFK6")
    monitor.events = []
    monitor.callbacks = []
    monitor._append_mts_event = lambda name, **fields: monitor.events.append((name, fields))
    monitor.order_mgr.register_callback("on_fill", monitor.callbacks.append)
    return monitor


def _submit_real_combo(monitor, action="BUY_NEAR_SELL_FAR", far_price=49770.0):
    buy_near = action == "BUY_NEAR_SELL_FAR"
    monitor._submit_mts_combo_entry(
        trade_id="REGRESSION-PAPER", action=action, reason="real callsite regression",
        near_code="TMFJ6", far_code="TMFK6",
        near_side=OrderSide.BUY if buy_near else OrderSide.SELL,
        far_side=OrderSide.SELL if buy_near else OrderSide.BUY,
        near_price=49592.0, far_price=far_price,
        ts=datetime(2026, 10, 5, 8, 46, 36), snapshot={},
        bar_dict={"spread": -178.0, "bbo_near_bid": 49591.0,
                  "bbo_near_ask": 49595.0, "bbo_far_bid": 49759.0,
                  "bbo_far_ask": 49768.0, "bbo_near_age_ms": 33,
                  "bbo_far_age_ms": 21}, entry_offset_ticks=10)
    orders = list(monitor.order_mgr.active_orders.values()) + monitor.order_mgr.completed
    assert len(orders) == 1
    return orders[0]


@pytest.mark.parametrize("action,net", [
    ("BUY_NEAR_SELL_FAR", -178.0), ("SELL_NEAR_BUY_FAR", 178.0)])
def test_real_paper_submit_gateway_to_simulated_combo_fill(real_paper_monitor, monkeypatch, action, net):
    monitor = real_paper_monitor

    def forbidden_broker_read(*args, **kwargs):
        pytest.fail("PAPER must not request a broker snapshot")

    monkeypatch.setattr(monitor, "_capture_post_startup_snapshot", forbidden_broker_read)
    assert monitor._reconcile_pending_mts_combos() == {"status": "SKIPPED"}
    order = _submit_real_combo(monitor, action)
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity == 1
    assert order.fills[0].fill_price == net
    assert order.fills[0].deal_id.startswith("PAPER-")
    assert order.truth_source == "paper_strategy"
    metadata = monitor._mts_pending_fills["REGRESSION-PAPER"]
    assert metadata["near_fill_price"] == 49592.0
    assert metadata["far_fill_price"] == 49770.0
    assert len(monitor.callbacks) == 1
    assert monitor.paper_fill_sim.get_pending_count() == 0
    names = [name for name, _ in monitor.events]
    assert "COMBO_ORDER_SUBMITTED" in names
    assert "ENTRY_SUBMITTED" in names
    assert "COMBO_ORDER_BROKER_CONFIRMED" not in names
    monitor._process_pending_paper_fills(49593.0, 49771.0, datetime(2026, 10, 5, 8, 47))
    monitor.paper_fill_sim.register_combo(order, near_symbol="TMFJ6", far_symbol="TMFK6")
    assert len(order.fills) == len(monitor.callbacks) == 1
    assert monitor.paper_fill_sim.get_pending_count() == 0


@pytest.mark.parametrize("invalid_far", [0.0, float("nan")])
def test_real_paper_tick_poll_waits_for_both_valid_legs(real_paper_monitor, invalid_far):
    monitor = real_paper_monitor
    order = _submit_real_combo(monitor, far_price=invalid_far)
    assert order.status is OrderStatus.SUBMITTED
    assert order.fills == []
    assert monitor.paper_fill_sim.get_pending_count() == 1
    monitor._process_pending_paper_fills(49592.0, 49770.0, datetime(2026, 10, 5, 8, 47))
    assert order.status is OrderStatus.FILLED
    assert len(order.fills) == len(monitor.callbacks) == 1
    assert monitor.paper_fill_sim.get_pending_count() == 0


def test_real_paper_simulator_partial_fills_have_distinct_local_identity(real_paper_monitor):
    monitor = real_paper_monitor
    order = monitor.order_mgr.create_order(
        symbol="TMFJ6-TMFK6", side=OrderSide.BUY,
        order_type=OrderType.MKP, quantity=2, price=-178.0,
        combo_legs=[{"symbol": "TMFJ6", "side": "buy", "quantity": 2},
                    {"symbol": "TMFK6", "side": "sell", "quantity": 2}],
        combo_strategy="TIME_SPREAD")
    monitor.order_mgr.submit(order)
    monitor.paper_fill_sim.register_combo(order, near_symbol="TMFJ6", far_symbol="TMFK6",
                                         metadata={"spread_side": "BUY_NEAR_SELL_FAR"})
    ts = datetime(2026, 10, 5, 8, 47)
    monitor._process_pending_paper_fills(49592.0, 49770.0, ts)
    assert order.status is OrderStatus.PARTIAL_FILLED
    monitor._process_pending_paper_fills(49592.0, 49770.0, ts)
    assert order.status is OrderStatus.FILLED
    ids = [fill.deal_id for fill in order.fills]
    assert len(set(ids)) == len(monitor.callbacks) == 2
    assert all(identity.startswith("PAPER-") for identity in ids)
    audits = [event for event in order.raw_events if event["type"] == "deal_fill"]
    assert len(audits) == 2
    assert all(event["source"] == "paper_simulator" for event in audits)
    assert all(event["observation_type"] == "PAPER_FILL" for event in audits)


@pytest.mark.parametrize("mode", ["live", "unknown"])
def test_local_on_fill_never_manufactures_live_receipt(real_paper_monitor, mode):
    manager = real_paper_monitor.order_mgr
    manager.mode = mode
    order = Order(symbol="TMFJ6", side=OrderSide.BUY, order_type=OrderType.MKP, quantity=1)
    order.submit("BROKER-EXISTING-RECEIPT")
    manager.active_orders[order.order_id] = order
    manager.on_fill(order.order_id, fill_price=49592.0, fill_qty=1, partial=False)
    assert order.fills == []
    assert order.status is OrderStatus.SUBMITTED
    assert order.raw_events[-1]["reason"] == "BROKER_RECEIPT_IDENTITY_MISSING"


def test_unidentified_deal_observation_still_rejected_in_paper(real_paper_monitor):
    manager = real_paper_monitor.order_mgr
    order = manager.create_order(symbol="TMFJ6", side=OrderSide.BUY,
                                 order_type=OrderType.MKP, quantity=1)
    manager.submit(order)
    result = manager.apply_deal_fill(order.order_id, fill_price=49592.0, fill_qty=1)
    assert result is None
    assert order.fills == []
    assert order.raw_events[-1]["reason"] == "BROKER_RECEIPT_IDENTITY_MISSING"


def test_live_submit_still_waits_for_real_reconciliation(real_paper_monitor, monkeypatch):
    # PAPER transport isolates external effects; LIVE monitor authority and the
    # real reconciliation implementation remain exercised, with unavailable
    # broker capture supplied only at the external snapshot boundary.
    monitor = real_paper_monitor
    monitor.live_trading = True
    monkeypatch.setattr(monitor, "_capture_mts_position_baseline", lambda: [])
    monkeypatch.setattr(monitor, "_capture_post_startup_snapshot",
                        lambda: {"fetch_status": {"capture": "FAIL"}})
    order = _submit_real_combo(monitor)
    assert order.status is OrderStatus.SUBMITTED
    assert order.fills == []
    assert monitor.paper_fill_sim.get_pending_count() == 0
    names = [name for name, _ in monitor.events]
    assert "COMBO_ORDER_BROKER_QUERY_UNAVAILABLE" in names
    assert "ENTRY_SUBMITTED" not in names
