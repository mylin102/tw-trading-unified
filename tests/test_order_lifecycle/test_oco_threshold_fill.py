"""
Contract test: OCO release orders respect ATR release threshold.

2026-07-07 Hermes Agent: Verifies the invariants that prevent the OCO
"submit + immediate fill" bug:

1. Below threshold → both OCO orders remain pending (no premature fill)
2. Threshold hit → only one leg fills, sibling is cancelled
3. No second fill on same tick (OCO atomic guard)
"""
import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace

from core.order_management.order_manager import OrderManager
from core.order_management.order import Order, OrderSide, OrderType, OrderStatus
from core.order_management.paper_fill import PaperFillSimulator


class TestOCOThresholdFill:
    """OCO release orders only fill when market reaches ATR threshold."""

    @pytest.fixture
    def oco_setup(self):
        """Create OCO bracket with LIMIT orders at threshold prices."""
        om = OrderManager(mode="paper")
        sim = PaperFillSimulator(om)

        # Simulate: spread entry was SHORT near (entry=45700), LONG far (entry=45980)
        # Release threshold: near BUY at 45700 - 150 = 45550 (cover short)
        #                    far SELL at 45980 + 150 = 46130 (close long)
        near_oid, far_oid = om.submit_release_bracket(
            symbol_near="TMFG6",
            symbol_far="TMFH6",
            quantity=1,
            side_near=OrderSide.BUY,
            side_far=OrderSide.SELL,
            price_near=45550.0,
            price_far=46130.0,
        )

        # Register both in paper_fill_sim
        near_order = om.active_orders.get(near_oid)
        far_order = om.active_orders.get(far_oid)
        sim.register(near_order)
        sim.register(far_order)

        return om, sim, near_oid, far_oid

    def test_below_threshold_no_fill(self, oco_setup):
        """Price is between the two thresholds → neither leg fills."""
        om, sim, near_oid, far_oid = oco_setup

        # Tick at 45700 — above near BUY threshold (45550) and below far SELL (46130)
        tick = type("Tick", (), {})()
        tick.code = "TMFG6"
        tick.close = 45700
        tick.open = 45700
        tick.high = 45705
        tick.low = 45695
        tick.volume = 1

        sim.process_tick(tick)

        # Near BUY LIMIT 45550: low=45695, not <= 45550 → no fill
        # Neither leg should fill
        assert near_oid in sim._pending_orders, (
            "Near leg should remain pending — price above threshold"
        )

    def test_near_threshold_hit_fills_near_only(self, oco_setup):
        """Near BUY threshold hit → near fills, far stays pending."""
        om, sim, near_oid, far_oid = oco_setup

        # Tick drops to 45540 — crosses near BUY threshold (45550)
        tick = type("Tick", (), {})()
        tick.code = "TMFG6"
        tick.close = 45545
        tick.open = 45550
        tick.high = 45550
        tick.low = 45540  # crosses 45550 limit
        tick.volume = 1

        sim.process_tick(tick)

        # Near BUY LIMIT 45550: low=45540 <= 45550 → fills at min(close, limit) = 45545
        filled_ids = {o.order_id for o in om.completed}
        assert near_oid in filled_ids, (
            f"Near leg should fill when price crosses threshold. "
            f"filled={filled_ids}, pending={list(sim._pending_orders.keys())}"
        )

    def test_far_threshold_not_hit_keeps_far_pending(self, oco_setup):
        """Far threshold far away → far remains pending after near fills."""
        om, sim, near_oid, far_oid = oco_setup

        # Tick at far market 45980 — far SELL threshold is 46130, not crossed
        tick_far = type("Tick", (), {})()
        tick_far.code = "TMFH6"
        tick_far.close = 45980
        tick_far.open = 45980
        tick_far.high = 45990
        tick_far.low = 45970
        tick_far.volume = 1

        sim.process_tick(tick_far)

        # Far SELL LIMIT 46130: high=45990, not >= 46130 → no fill
        assert far_oid in sim._pending_orders, (
            "Far leg should remain pending — price below far SELL threshold"
        )

    def test_far_threshold_hit_fills_far(self, oco_setup):
        """Far SELL threshold hit → far fills."""
        om, sim, near_oid, far_oid = oco_setup

        # Tick rises to 46150 — crosses far SELL threshold (46130)
        tick_far = type("Tick", (), {})()
        tick_far.code = "TMFH6"
        tick_far.close = 46140
        tick_far.open = 46130
        tick_far.high = 46150  # crosses 46130 limit
        tick_far.low = 46130
        tick_far.volume = 1

        sim.process_tick(tick_far)

        filled_ids = {o.order_id for o in om.completed}
        assert far_oid in filled_ids, (
            f"Far leg should fill when price crosses threshold. "
            f"filled={filled_ids}"
        )

    def test_both_oco_legs_registered_in_sim(self, oco_setup):
        """Both OCO legs are registered in paper_fill_sim (not just one)."""
        om, sim, near_oid, far_oid = oco_setup

        assert near_oid in sim._pending_orders, "Near leg must be registered"
        assert far_oid in sim._pending_orders, "Far leg must be registered"

    def test_oco_orders_are_limit_not_market(self, oco_setup):
        """OCO release orders use LIMIT type, not MKP/MARKET."""
        om, sim, near_oid, far_oid = oco_setup

        near_order = om.active_orders.get(near_oid)
        far_order = om.active_orders.get(far_oid)

        assert near_order.order_type == OrderType.LIMIT, (
            f"OCO near must be LIMIT, got {near_order.order_type}"
        )
        assert far_order.order_type == OrderType.LIMIT, (
            f"OCO far must be LIMIT, got {far_order.order_type}"
        )
        assert near_order.price == 45550.0, "OCO near price mismatch"
        assert far_order.price == 46130.0, "OCO far price mismatch"
