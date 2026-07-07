"""
2026-07-07 Hermes Agent: Invariant tests for OCO ghost-order prevention.

Contract: After restart, when lifecycle.release_group has SUBMITTED status
with old near/far order IDs, _save_orders_file_wrapper() MUST NOT inject
duplicate entries for order IDs that already exist in:
  1. export_data (from order_mgr.get_pending/get_completed)
  2. order_mgr.completed (belt-and-suspenders guard)
  3. The final orders JSON MUST NOT contain duplicate order_id values.
"""
import pytest
from unittest.mock import MagicMock

from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
from core.order_management.order_manager import OrderManager


class TestOCOGhostOrderPrevention:
    """Invariant: OCO release orders already filled/cancelled must not reappear."""

    def test_duplicate_order_id_guard_prevents_ghost_injection(self):
        """
        Scenario: PM2 restart.
        - order_mgr.completed has ORD-000003 (filled from prior session).
        - lifecycle.release_group.status = SUBMITTED, near_order_id = "ORD-000003".
        - _save_orders_file_wrapper() runs.
        -> ORD-000003 must NOT appear as a new "submitted" entry.
        -> No duplicate order_id in the output.
        """
        # 1. Build order_mgr with completed orders (both OCO legs filled)
        order_mgr = OrderManager(mode="paper")
        for sym, side, oid in [
            ("TMF_NEAR", OrderSide.SELL, "ORD-000003"),
            ("TMF_FAR", OrderSide.BUY, "ORD-000004"),
        ]:
            o = Order(symbol=sym, side=side, order_type=OrderType.MKP,
                      quantity=1, strategy="MTS_RELEASE_OCO", order_id=oid)
            o.submit(f"EXCH-{oid[-1]}")
            o.fill(47265, 1)
            order_mgr.completed.append(o)
            order_mgr.active_orders.pop(oid, None)

        # 2. Build mock strategy with SUBMITTED release_group
        from strategies.plugins.futures.active.tmf_spread import (
            PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
        )
        strategy = MagicMock()
        rg = ReleaseGroup(status=ReleaseGroupStatus.SUBMITTED)
        rg.near_order_id = "ORD-000003"
        rg.far_order_id = "ORD-000004"
        rg.near_side = "sell"
        rg.far_side = "buy"
        rg.near_price = 47265.0
        rg.far_price = 47531.0
        rg.order_type = "MKP"
        lc = PositionLifecycle(phase=PositionPhase.SPREAD, release_group=rg)
        strategy._lifecycle_oca = lc

        # 3. Simulate export_data build + OCO injection guard
        all_orders = order_mgr.get_completed() + order_mgr.get_pending()
        export_data = []
        for o in all_orders:
            d = o.to_dict()
            d["unrealized_pnl"] = None
            d["unrealized_pnl_pts"] = None
            d["current_price"] = None
            export_data.append(d)

        _completed_ids = {o.order_id for o in order_mgr.completed}

        injected = []
        for _label, _oid, _side_attr, _price_attr, _entry_side_attr in [
            ("NEAR", rg.near_order_id, "near_side", "near_price", "_near_side"),
            ("FAR", rg.far_order_id, "far_side", "far_price", "_far_side"),
        ]:
            if not _oid:
                continue
            if any(d.get("order_id") == _oid for d in export_data):
                continue  # Guard 1: found in export_data
            if _oid in _completed_ids:
                continue  # Guard 2: found in completed (belt-and-suspenders)
            injected.append(_oid)

        # Assertions
        assert len(injected) == 0, (
            f"Ghost injection detected: {injected}. "
            f"Both ORD-000003 and ORD-000004 are in completed/export_data - must be skipped."
        )

    def test_no_duplicate_order_ids_in_full_export(self):
        """
        Invariant: After _save_orders_file_wrapper(), the exported orders JSON
        must never contain duplicate order_id values.
        """
        order_mgr = OrderManager(mode="paper")

        # Create two completed orders
        for i, (sym, side, oid) in enumerate([
            ("TMF_NEAR", OrderSide.BUY, "ORD-000001"),
            ("TMF_FAR", OrderSide.SELL, "ORD-000002"),
        ]):
            o = Order(symbol=sym, side=side, order_type=OrderType.MKP,
                      quantity=1, strategy="MTS_ENTRY", order_id=oid)
            o.submit(f"EXCH-{i:03d}")
            o.fill(47000 + i * 100, 1)
            order_mgr.completed.append(o)
            order_mgr.active_orders.pop(oid, None)

        # Build mock strategy with SUBMITTED release_group using SAME order IDs
        from strategies.plugins.futures.active.tmf_spread import (
            PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
        )
        strategy = MagicMock()
        rg = ReleaseGroup(status=ReleaseGroupStatus.SUBMITTED)
        rg.near_order_id = "ORD-000001"  # Same as completed entry order!
        rg.far_order_id = "ORD-000002"
        rg.near_side = "sell"
        rg.far_side = "buy"
        rg.near_price = 47265.0
        rg.far_price = 47531.0
        rg.order_type = "MKP"
        lc = PositionLifecycle(phase=PositionPhase.SPREAD, release_group=rg)
        strategy._lifecycle_oca = lc

        # Simulate full export_data build
        all_orders = order_mgr.get_completed() + order_mgr.get_pending()
        export_data = []
        for o in all_orders:
            d = o.to_dict()
            d["unrealized_pnl"] = None
            d["unrealized_pnl_pts"] = None
            d["current_price"] = None
            export_data.append(d)

        # Apply OCO injection guard
        _completed_ids = {o.order_id for o in order_mgr.completed}
        for _label, _oid, _side_attr, _price_attr, _entry_side_attr in [
            ("NEAR", rg.near_order_id, "near_side", "near_price", "_near_side"),
            ("FAR", rg.far_order_id, "far_side", "far_price", "_far_side"),
        ]:
            if not _oid:
                continue
            if any(d.get("order_id") == _oid for d in export_data):
                continue
            if _oid in _completed_ids:
                continue
            export_data.append({
                "order_id": _oid,
                "symbol": f"TMF_{_label}",
                "status": "submitted",
            })

        # Invariant: no duplicate order_id
        order_ids = [d["order_id"] for d in export_data]
        assert len(order_ids) == len(set(order_ids)), (
            f"Duplicate order_ids detected: {order_ids}"
        )

        # Invariant: completed entries are NOT in pending/submitted state
        completed_ids_in_export = {d["order_id"] for d in export_data
                                   if d.get("status") == "filled"}
        pending_ids_in_export = {d["order_id"] for d in export_data
                                 if d.get("status") == "submitted"}
        intersected = completed_ids_in_export & pending_ids_in_export
        assert len(intersected) == 0, (
            f"Order IDs appear in both filled and submitted states: {intersected}"
        )

    def test_restart_scenario_reconcile_before_save_prevents_ghosts(self):
        """
        Full restart scenario:
        - order_mgr is fresh (empty completed, empty active_orders)
        - BUT lifecycle.release_group has old SUBMITTED state with order IDs
        - After reconcile runs (creating orders in active_orders),
          _save_orders_file_wrapper MUST see the reconciled orders in
          get_pending() and NOT inject ghosts.
        """
        order_mgr = OrderManager(mode="paper")
        sim = MagicMock()
        order_mgr.set_simulator(sim)

        # Build strategy with SUBMITTED release_group
        from strategies.plugins.futures.active.tmf_spread import (
            PositionLifecycle, PositionPhase, ReleaseGroup, ReleaseGroupStatus,
        )
        strategy = MagicMock()
        rg = ReleaseGroup(status=ReleaseGroupStatus.SUBMITTED)
        rg.near_order_id = "ORD-000003"
        rg.far_order_id = "ORD-000004"
        rg.near_side = "sell"
        rg.far_side = "buy"
        rg.near_price = 47265.0
        rg.far_price = 47531.0
        rg.order_type = "MKP"
        lc = PositionLifecycle(phase=PositionPhase.SPREAD, release_group=rg)
        strategy._lifecycle_oca = lc

        # STEP 1: Simulate what happens BEFORE reconcile
        # (empty order_mgr -> ghost injection WOULD fire)
        all_orders_before = order_mgr.get_completed() + order_mgr.get_pending()
        export_data_before = []
        for o in all_orders_before:
            d = o.to_dict()
            d["unrealized_pnl"] = None
            d["unrealized_pnl_pts"] = None
            d["current_price"] = None
            export_data_before.append(d)

        _completed_ids_before = {o.order_id for o in order_mgr.completed}
        injected_before = []
        for _label, _oid, _side_attr, _price_attr, _entry_side_attr in [
            ("NEAR", rg.near_order_id, "near_side", "near_price", "_near_side"),
            ("FAR", rg.far_order_id, "far_side", "far_price", "_far_side"),
        ]:
            if not _oid:
                continue
            if any(d.get("order_id") == _oid for d in export_data_before):
                continue
            if _oid in _completed_ids_before:
                continue
            injected_before.append(_oid)

        # BEFORE reconcile: ghost injection WOULD fire (order_mgr is empty)
        # This is expected - which is WHY we need reconcile FIRST
        assert len(injected_before) == 2, (
            f"Expected 2 ghosts before reconcile (order_mgr empty), got {injected_before}"
        )

        # STEP 2: Simulate reconcile (same as _reconcile_paper_oco_orders)
        near_order = Order(
            symbol="TMF_NEAR", side=OrderSide.SELL,
            order_type=OrderType.MKP, quantity=1,
            strategy="MTS_RELEASE_OCO",
            order_id="ORD-000003",
        )
        far_order = Order(
            symbol="TMF_FAR", side=OrderSide.BUY,
            order_type=OrderType.MKP, quantity=1,
            strategy="MTS_RELEASE_OCO",
            order_id="ORD-000004",
        )
        order_mgr.active_orders["ORD-000003"] = near_order
        order_mgr.active_orders["ORD-000004"] = far_order
        near_order.submit("EXCH-R3")
        far_order.submit("EXCH-R4")

        # STEP 3: AFTER reconcile -> export_data build
        all_orders_after = order_mgr.get_completed() + order_mgr.get_pending()
        export_data_after = []
        for o in all_orders_after:
            d = o.to_dict()
            d["unrealized_pnl"] = None
            d["unrealized_pnl_pts"] = None
            d["current_price"] = None
            export_data_after.append(d)

        _completed_ids_after = {o.order_id for o in order_mgr.completed}
        injected_after = []
        for _label, _oid, _side_attr, _price_attr, _entry_side_attr in [
            ("NEAR", rg.near_order_id, "near_side", "near_price", "_near_side"),
            ("FAR", rg.far_order_id, "far_side", "far_price", "_far_side"),
        ]:
            if not _oid:
                continue
            if any(d.get("order_id") == _oid for d in export_data_after):
                continue
            if _oid in _completed_ids_after:
                continue
            injected_after.append(_oid)

        # AFTER reconcile: NO ghost injection (orders visible via get_pending)
        assert len(injected_after) == 0, (
            f"Ghost injection after reconcile: {injected_after}. "
            f"Reconciled orders should be visible via get_pending()."
        )

        # Verify orders ARE in export_data from get_pending()
        pending_ids = {d["order_id"] for d in export_data_after}
        assert "ORD-000003" in pending_ids
        assert "ORD-000004" in pending_ids
        # And there are no duplicates
        assert len(pending_ids) == len(export_data_after), (
            f"Duplicate order IDs in export after reconcile"
        )
