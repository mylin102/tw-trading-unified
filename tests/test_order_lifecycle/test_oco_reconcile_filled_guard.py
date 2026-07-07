"""
Contract test: OCO reconcile does NOT re-register orders after filled_leg is set.

2026-07-07 Hermes Agent: The P0 guard that breaks the reconcile→fill→reconcile→fill
infinite loop.  Once release_group.filled_leg is non-None, reconciliation must skip
entirely without touching paper_fill_sim.
"""
import pytest
from unittest.mock import MagicMock
from types import SimpleNamespace

from core.order_management.order_manager import OrderManager
from core.order_management.order import Order, OrderSide, OrderType, OrderStatus
from core.order_management.paper_fill import PaperFillSimulator
from strategies.plugins.futures.active.tmf_spread import (
    ReleaseGroup, ReleaseGroupStatus, Leg, PositionLifecycle,
)


@pytest.fixture
def monitor_with_oco_state(monkeypatch, tmp_path):
    """Build a minimal FuturesMonitor with paper_fill_sim and strategy."""
    from strategies.futures.monitor import FuturesMonitor

    m = FuturesMonitor.__new__(FuturesMonitor)

    om = OrderManager(mode="paper")
    sim = PaperFillSimulator(om)
    object.__setattr__(om, "_sim", sim)

    object.__setattr__(m, "order_mgr", om)
    object.__setattr__(m, "paper_fill_sim", sim)
    object.__setattr__(m, "ticker", "TMF")
    object.__setattr__(m, "contract", SimpleNamespace(code="TMFG6"))
    object.__setattr__(m, "far_contract", SimpleNamespace(code="TMFH6"))

    # Strategy: release_group with filled_leg initially None
    rg = ReleaseGroup(
        status=ReleaseGroupStatus.SUBMITTED,
        near_order_id="ORD-000003",
        far_order_id="ORD-000004",
        near_side="sell",
        far_side="buy",
        order_type="MKP",
    )
    lc = PositionLifecycle()
    lc.release_group = rg

    strategy = SimpleNamespace()
    strategy._lifecycle_oca = lc
    strategy._near_entry = 45700.0
    strategy._far_entry = 45980.0
    strategy._near_side = "LONG"
    strategy._far_side = "SHORT"
    strategy._near_last = 45700.0
    strategy._far_last = 45980.0
    strategy._trade_id = "test-trade"
    strategy._last_atr = 100.0
    strategy._release_stop_fixed = 150.0
    strategy._trail_dist_fixed = 20.0
    strategy._entry_z = 3.0
    strategy._has_position = True

    reg = {"tmf_spread": strategy}
    object.__setattr__(m, "_registry", reg)

    return m, sim, rg


class TestOCOReconcileSkipsAfterFilledLeg:
    """P0: Once filled_leg is set, reconcile must NOT touch paper_fill_sim."""

    def test_filled_leg_none_allows_reconcile(self, monitor_with_oco_state):
        """filled_leg=None → reconciliation proceeds (normal first call)."""
        m, sim, rg = monitor_with_oco_state
        strategy = m._registry["tmf_spread"]

        assert rg.filled_leg is None
        assert rg.status == ReleaseGroupStatus.SUBMITTED

        # Register nothing in sim → reconciliation should re-register
        assert len(sim._pending_orders) == 0
        assert len(sim.consumed_order_ids) == 0

        result = m._reconcile_paper_oco_orders(strategy)
        # Since orders aren't in active or pending, reconciliation re-registers them
        assert "ORD-000003" in sim._pending_orders or "ORD-000003" in m.order_mgr.active_orders

    def test_filled_leg_set_blocks_reconcile(self, monitor_with_oco_state):
        """filled_leg=NEAR → reconciliation returns immediately, sim untouched."""
        m, sim, rg = monitor_with_oco_state
        strategy = m._registry["tmf_spread"]

        # Set filled_leg — OCO has been partially resolved
        rg.filled_leg = Leg.NEAR

        # Clear sim state first
        sim._pending_orders.clear()
        sim.consumed_order_ids.clear()

        before_pending = len(sim._pending_orders)
        before_consumed = len(sim.consumed_order_ids)

        result = m._reconcile_paper_oco_orders(strategy)

        # Reconciliation must be a no-op
        assert result is None
        assert len(sim._pending_orders) == before_pending, (
            f"_pending_orders changed from {before_pending} to {len(sim._pending_orders)}"
        )
        assert len(sim.consumed_order_ids) == before_consumed

    def test_filled_leg_set_no_register_called(self, monitor_with_oco_state):
        """filled_leg set → register() is never called on paper_fill_sim."""
        m, sim, rg = monitor_with_oco_state
        strategy = m._registry["tmf_spread"]

        rg.filled_leg = Leg.NEAR

        sim._pending_orders.clear()
        sim.consumed_order_ids.clear()

        # Spy on register
        original_register = sim.register
        call_count = [0]

        def spy_register(order):
            call_count[0] += 1
            return original_register(order)

        sim.register = spy_register

        m._reconcile_paper_oco_orders(strategy)

        assert call_count[0] == 0, (
            f"register() called {call_count[0]} times — should be 0 when filled_leg is set"
        )

    def test_filled_leg_still_blocks_after_status_check_passes(self, monitor_with_oco_state):
        """Even if release_group.status=SUBMITTED, filled_leg!=None still blocks."""
        m, sim, rg = monitor_with_oco_state
        strategy = m._registry["tmf_spread"]

        # Both status=SUBMITTED AND filled_leg=NEAR set
        rg.status = ReleaseGroupStatus.SUBMITTED
        rg.filled_leg = Leg.NEAR

        sim._pending_orders.clear()
        before = len(sim._pending_orders)

        m._reconcile_paper_oco_orders(strategy)

        assert len(sim._pending_orders) == before, (
            "reconcile should be blocked by filled_leg, even when status=SUBMITTED"
        )
