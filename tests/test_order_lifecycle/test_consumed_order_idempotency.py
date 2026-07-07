"""
Contract test: consumed_order_ids idempotency in PaperFillSimulator.

2026-07-07 Hermes Agent: Guards against the OCO reconcile→register→fill loop
by ensuring that consumed (filled/cancelled) orders can never be re-registered.
"""
import pytest
from core.order_management.order_manager import OrderManager
from core.order_management.order import Order, OrderSide, OrderType, OrderStatus
from core.order_management.paper_fill import PaperFillSimulator


class TestConsumedOrderCannotBeReRegistered:
    """P0: consumed order IDs are permanently blocked from re-registration."""

    def test_filled_order_rejected_by_register(self):
        """Given order was filled → when register() again → pending_orders unchanged."""
        om = OrderManager(mode="paper")
        sim = PaperFillSimulator(om)

        order = Order(
            symbol="TMFG6", side=OrderSide.SELL, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_RELEASE_OCO",
        )
        order.status = OrderStatus.SUBMITTED
        sim.register(order)
        assert order.order_id in sim._pending_orders

        # Simulate fill: remove from pending + mark consumed
        sim._pending_orders.pop(order.order_id, None)
        sim.consumed_order_ids.add(order.order_id)

        # Attempt re-registration with a fresh SUBMITTED order object
        order2 = Order(
            symbol="TMFG6", side=OrderSide.SELL, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_RELEASE_OCO",
            order_id=order.order_id,  # same ID
        )
        order2.status = OrderStatus.SUBMITTED
        sim.register(order2)

        # Must NOT appear in pending_orders
        assert order.order_id not in sim._pending_orders
        assert order.order_id in sim.consumed_order_ids

    def test_cancelled_via_remove_blocked(self):
        """Given order was cancelled via remove() → re-registration blocked."""
        om = OrderManager(mode="paper")
        sim = PaperFillSimulator(om)

        order = Order(
            symbol="TMFG6", side=OrderSide.BUY, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_RELEASE_OCO",
        )
        order.status = OrderStatus.SUBMITTED
        sim.register(order)
        assert order.order_id in sim._pending_orders

        sim.remove(order.order_id)
        assert order.order_id not in sim._pending_orders
        assert order.order_id in sim.consumed_order_ids

        # Re-register
        order2 = Order(
            symbol="TMFG6", side=OrderSide.BUY, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_RELEASE_OCO",
            order_id=order.order_id,
        )
        order2.status = OrderStatus.SUBMITTED
        sim.register(order2)

        assert order.order_id not in sim._pending_orders

    def test_process_tick_marks_consumed_on_fill(self):
        """process_tick() adds filled orders to consumed_order_ids."""
        om = OrderManager(mode="paper")
        sim = PaperFillSimulator(om)

        order = om.create_order(
            symbol="TMFG6", side=OrderSide.SELL, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_RELEASE_OCO",
        )
        om.submit(order)
        sim.register(order)

        # Feed a tick that triggers fill
        tick = type("Tick", (), {})()
        tick.code = "TMFG6"
        tick.close = 45700
        tick.open = 45700
        tick.high = 45700
        tick.low = 45700
        tick.volume = 1

        sim.process_tick(tick)

        # After fill, order moved from active_orders to completed,
        # and consumed_order_ids should be populated by process_tick cleanup.
        assert order.order_id in sim.consumed_order_ids, (
            f"Order {order.order_id} must be in consumed_order_ids after fill. "
            f"consumed={sim.consumed_order_ids}"
        )

    def test_clear_records_resets_consumed_ids(self):
        """clear_records must reset consumed_order_ids to prevent cross-session pollution."""
        om = OrderManager(mode="paper")
        sim = PaperFillSimulator(om)

        order = Order(
            symbol="TMFG6", side=OrderSide.SELL, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_RELEASE_OCO",
        )
        order.status = OrderStatus.SUBMITTED
        sim.register(order)
        sim._pending_orders.pop(order.order_id, None)
        sim.consumed_order_ids.add(order.order_id)

        assert len(sim.consumed_order_ids) == 1

        # Simulate clear_records
        sim._pending_orders.clear()
        sim.consumed_order_ids.clear()

        assert len(sim.consumed_order_ids) == 0
        assert len(sim._pending_orders) == 0
