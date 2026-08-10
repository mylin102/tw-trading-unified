"""Regression coverage for the live MTS order-manager adapter boundary."""

from types import SimpleNamespace

from core.order_management.order import OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager
from strategies.futures.squeeze_futures.data.shioaji_client import ShioajiClient


def _new_order(manager):
    return manager.create_order(
        symbol="TMFH6", side=OrderSide.SELL, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_ENTRY",
    )


def test_shioaji_object_bridge_passes_contract_action_and_quantity():
    """The domain Order never reaches the positional broker API directly."""
    client = ShioajiClient.__new__(ShioajiClient)
    contract = SimpleNamespace(code="TMFH6")
    received = {}
    client.get_contract = lambda symbol: contract

    def place_order(actual_contract, action, quantity, price=0):
        received.update(contract=actual_contract, action=action,
                        quantity=quantity, price=price)
        return "receipt"

    client.place_order = place_order
    manager = OrderManager(mode="paper")
    result = client.place_order_object(_new_order(manager))

    assert result == "receipt"
    assert received == {
        "contract": contract, "action": "sell", "quantity": 1, "price": 0,
    }


def test_live_adapter_exception_rejects_without_active_pending_order():
    """A local adapter error is terminal; it cannot become a watchdog timeout."""
    class FailingAdapter:
        def place_order_object(self, order):
            raise TypeError("missing positional arguments")

    manager = OrderManager(mode="live", broker_adapter=FailingAdapter())
    order = _new_order(manager)

    assert manager.submit(order) is False
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == "ADAPTER_SUBMIT_FAILED"
    assert order.order_id not in manager.active_orders
    assert manager.completed == [order]


def test_live_missing_broker_receipt_rejects_without_active_pending_order():
    """A response without broker identity is not a submitted exchange order."""
    class ReceiptlessAdapter:
        def place_order_object(self, order):
            return SimpleNamespace(status=SimpleNamespace())

    manager = OrderManager(mode="live", broker_adapter=ReceiptlessAdapter())
    order = _new_order(manager)

    assert manager.submit(order) is False
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == "BROKER_RECEIPT_MISSING"
    assert order.order_id not in manager.active_orders
    assert manager.completed == [order]
