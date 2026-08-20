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
