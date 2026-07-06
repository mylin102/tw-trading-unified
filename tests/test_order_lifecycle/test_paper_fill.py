"""
V-Model Level 1: Unit Tests for PaperFillSimulator

模擬 Paper Mode 的撮合引擎：
- 市價單：下一筆 tick 價格即成交
- 限價單：價格穿過限價即成交
- 部分成交：大單拆 2-3 筆 fill
"""
import pytest
from unittest.mock import MagicMock
from datetime import datetime

from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
from core.order_management.order_manager import OrderManager
from core.order_management.paper_fill import PaperFillSimulator


@pytest.fixture
def mgr():
    """Paper OrderManager with PaperFillSimulator"""
    mgr = OrderManager(mode="paper")
    sim = PaperFillSimulator(mgr)
    mgr.set_simulator(sim)
    return mgr, sim


@pytest.fixture
def tick():
    """模擬 tick 數據"""
    def _tick(timestamp, open, high, low, close, volume=100):
        t = MagicMock()
        t.datetime = timestamp
        t.open = open
        t.high = high
        t.low = low
        t.close = close
        t.volume = volume
        return t
    return _tick


# ── L1-UT-11: Market Order Fill ──

class TestMarketFill:
    def test_market_buy_fills_next_tick(self, mgr, tick):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        order_mgr.submit(order, exchange_ordno="P-001")
        sim.register(order)

        t = tick("2026-04-14 15:00:00", 36400, 36450, 36390, 36440)
        sim.process_tick(t)

        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 1
        assert order.avg_fill_price == 36440  # close price

    def test_market_sell_fills_next_tick(self, mgr, tick):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.SELL, OrderType.MARKET, 1)
        order_mgr.submit(order, exchange_ordno="P-002")
        sim.register(order)

        t = tick("2026-04-14 15:01:00", 36500, 36520, 36480, 36490)
        sim.process_tick(t)

        assert order.status == OrderStatus.FILLED
        assert order.avg_fill_price == 36490

    def test_market_order_not_registered_ignored(self, mgr, tick):
        order_mgr, sim = mgr
        # Create order but don't register with simulator
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        order_mgr.submit(order, exchange_ordno="P-003")
        # NOT registered

        t = tick("2026-04-14 15:02:00", 36400, 36450, 36390, 36440)
        sim.process_tick(t)

        # Order should remain SUBMITTED (not filled by simulator)
        assert order.status == OrderStatus.SUBMITTED


# ── L1-UT-12: Limit Order Fill ──

class TestLimitFill:
    def test_limit_buy_fills_when_price_crosses(self, mgr, tick):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.LIMIT, 1, price=36400)
        order_mgr.submit(order, exchange_ordno="P-010")
        sim.register(order)

        # Price above limit → no fill
        t1 = tick("2026-04-14 15:00:00", 36450, 36460, 36440, 36450)
        sim.process_tick(t1)
        assert order.status == OrderStatus.SUBMITTED

        # Price crosses below limit → fill
        t2 = tick("2026-04-14 15:01:00", 36420, 36430, 36380, 36390)
        sim.process_tick(t2)

        assert order.status == OrderStatus.FILLED
        assert order.avg_fill_price <= 36400  # Fill at limit or better

    def test_limit_sell_fills_when_price_crosses(self, mgr, tick):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.SELL, OrderType.LIMIT, 1, price=36500)
        order_mgr.submit(order, exchange_ordno="P-011")
        sim.register(order)

        # Price below limit → no fill
        t1 = tick("2026-04-14 15:00:00", 36480, 36490, 36470, 36480)
        sim.process_tick(t1)
        assert order.status == OrderStatus.SUBMITTED

        # Price crosses above limit → fill
        t2 = tick("2026-04-14 15:01:00", 36510, 36520, 36505, 36515)
        sim.process_tick(t2)

        assert order.status == OrderStatus.FILLED
        assert order.avg_fill_price >= 36500  # Fill at limit or better

    def test_limit_never_crosses_stays_submitted(self, mgr, tick):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.LIMIT, 1, price=30000)
        order_mgr.submit(order, exchange_ordno="P-012")
        sim.register(order)

        # Price stays well above limit
        for i in range(10):
            t = tick(f"2026-04-14 15:{i:02d}:00", 36400+i, 36420+i, 36380+i, 36410+i)
            sim.process_tick(t)

        assert order.status == OrderStatus.SUBMITTED


# ── L1-UT-13: Partial Fill ──

class TestPartialFillSim:
    def test_large_order_partially_filled(self, mgr, tick):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 5)
        order_mgr.submit(order, exchange_ordno="P-020")
        sim.register(order)

        t = tick("2026-04-14 15:00:00", 36400, 36450, 36390, 36440, volume=100)
        sim.process_tick(t)

        # Large order (5 lots) should be partially filled
        assert 0 < order.filled_quantity < 5
        assert order.status == OrderStatus.PARTIAL_FILLED

    def test_large_order_eventually_fills_complete(self, mgr, tick):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 5)
        order_mgr.submit(order, exchange_ordno="P-021")
        sim.register(order)

        # Process multiple ticks
        for i in range(20):
            if order.status == OrderStatus.FILLED:
                break
            t = tick(f"2026-04-14 15:{i:02d}:00", 36400+i, 36450+i, 36390+i, 36440+i, volume=100)
            sim.process_tick(t)

        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 5


# ── L1-UT-14: Order Removal After Cancel ──

class TestCancelRemoval:
    def test_cancelled_order_removed_from_sim(self, mgr, tick):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        order_mgr.submit(order, exchange_ordno="P-030")
        sim.register(order)

        assert order.order_id in sim._pending_orders

        order_mgr.cancel(order.order_id, reason="test")
        assert order.order_id not in sim._pending_orders

    def test_filled_order_removed_from_sim(self, mgr, tick):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        order_mgr.submit(order, exchange_ordno="P-031")
        sim.register(order)

        t = tick("2026-04-14 15:00:00", 36400, 36450, 36390, 36440)
        sim.process_tick(t)

        assert order.status == OrderStatus.FILLED
        assert order.order_id not in sim._pending_orders


# ── L1-UT-15: Edge Cases ──

class TestPaperFillEdges:
    def test_process_tick_with_empty_orders(self, mgr, tick):
        order_mgr, sim = mgr
        t = tick("2026-04-14 15:00:00", 36400, 36450, 36390, 36440)
        sim.process_tick(t)  # Should not crash

    def test_register_non_submitted_order(self, mgr):
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        # Not submitted yet
        sim.register(order)
        assert order.order_id not in sim._pending_orders

    def test_fill_price_uses_close(self, mgr, tick):
        """Market orders fill at close price"""
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        order_mgr.submit(order, exchange_ordno="P-040")
        sim.register(order)

        t = tick("2026-04-14 15:00:00", 36400, 36500, 36350, 36470, volume=200)
        sim.process_tick(t)

        assert order.avg_fill_price == 36470  # close price

    def test_limit_buy_fill_price_better_than_limit(self, mgr, tick):
        """Limit buy fills at close if close < limit (better price)"""
        order_mgr, sim = mgr
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.LIMIT, 1, price=36500)
        order_mgr.submit(order, exchange_ordno="P-041")
        sim.register(order)

        t = tick("2026-04-14 15:00:00", 36480, 36490, 36470, 36480)
        sim.process_tick(t)

        assert order.status == OrderStatus.FILLED
        assert order.avg_fill_price == 36480  # close, which is better than limit 36500


# ═════════════════════════════════════════════════════
# ADR-010 OCO Paper Fill Acceptance Tests (2026-07-06)
#
# NOTE: These test the raw PaperFillSimulator without the
# monitor's OCO callback wiring (_wire_order_callbacks →
# _check_oco_release_fill). In production, when both MKP
# orders are polled via _process_pending_paper_fills():
#   1. First leg fills → on_fill callback fires synchronously
#   2. _check_oco_release_fill cancels sibling via order_mgr.cancel()
#   3. cancel → simulator.remove(sibling) → sibling never fills
# So in production only ONE leg fills, the other is cancelled.
# ═════════════════════════════════════════════════════

class TestPaperFillSimulatorPolling:
    """Verify MKP orders fill correctly via process_tick (raw sim, no OCO callbacks)."""

    def test_mkp_orders_fill_on_valid_tick(self, mgr, tick):
        """Two MKP orders both fill when process_tick called with matching symbols.
        
        In production, the OCO callback cancels the sibling synchronously
        after the first fill, so only one fills. This test verifies the
        raw fill simulator correctly fills both when no OCO logic is wired.
        """
        order_mgr, sim = mgr

        near = order_mgr.create_order("TMF_NEAR", OrderSide.SELL, OrderType.MKP, 1,
                                       strategy="MTS_RELEASE_OCO")
        far = order_mgr.create_order("TMF_FAR", OrderSide.BUY, OrderType.MKP, 1,
                                      strategy="MTS_RELEASE_OCO")
        order_mgr.submit(near, exchange_ordno="PAPER-ORD-000003")
        order_mgr.submit(far, exchange_ordno="PAPER-ORD-000004")
        sim.register(near)
        sim.register(far)

        assert sim.get_pending_count() == 2

        # Use real objects (not MagicMock) so symbol guard works
        def _make_tick(code, close):
            t = type("Tick", (), {})()
            t.code = code
            t.datetime = datetime.now()
            t.close = close
            t.open = close
            t.high = close
            t.low = close
            t.volume = 50
            return t

        t_near = _make_tick("TMF_NEAR", 47237)
        t_far = _make_tick("TMF_FAR", 47473)

        sim.process_tick(t_near)
        sim.process_tick(t_far)

        # Both MKP orders should have filled (at close price)
        assert near.status == OrderStatus.FILLED, \
            f"near order should be FILLED, got {near.status}"
        assert far.status == OrderStatus.FILLED, \
            f"far order should be FILLED, got {far.status}"
        assert near.avg_fill_price == 47237
        assert far.avg_fill_price == 47473

    def test_mkp_oco_symbol_mismatch_skips(self, mgr):
        """Orders with mismatched tick symbol are skipped (guard check)."""
        order_mgr, sim = mgr

        near = order_mgr.create_order("TMF_NEAR", OrderSide.SELL, OrderType.MKP, 1,
                                       strategy="MTS_RELEASE_OCO")
        order_mgr.submit(near, exchange_ordno="PAPER-ORD-N1")
        sim.register(near)

        # Use real object with mismatched symbol
        def _make_tick(code, close):
            t = type("Tick", (), {})()
            t.code = code
            t.datetime = datetime.now()
            t.close = close
            t.open = close
            t.high = close
            t.low = close
            t.volume = 50
            return t

        t_wrong = _make_tick("TMF_FAR", 47237)  # mismatched
        sim.process_tick(t_wrong)

        # Order should NOT have filled
        assert near.status == OrderStatus.SUBMITTED, \
            f"order should remain SUBMITTED with mismatched symbol, got {near.status}"

    def test_pending_count_zero_skips_polling(self, mgr):
        """_process_pending_paper_fills returns early when no pending orders."""
        order_mgr, sim = mgr
        assert sim.get_pending_count() == 0
        # No crash expected
        sim.process_tick(None)

    def test_mkp_oco_first_fill_cancels_sibling_during_polling(self, mgr):
        """Acceptance: first MKP fill cancels sibling synchronously via OCO callback.

        Simulates the production flow where _wire_order_callbacks →
        _check_oco_release_fill cancels the sibling on first fill.
        After cancel, the sibling is removed from the simulator and
        will NOT fill on a subsequent tick.
        """
        order_mgr, sim = mgr

        near = order_mgr.create_order("TMF_NEAR", OrderSide.SELL, OrderType.MKP, 1,
                                       strategy="MTS_RELEASE_OCO")
        far = order_mgr.create_order("TMF_FAR", OrderSide.BUY, OrderType.MKP, 1,
                                      strategy="MTS_RELEASE_OCO")
        order_mgr.submit(near, exchange_ordno="PAPER-ORD-000003")
        order_mgr.submit(far, exchange_ordno="PAPER-ORD-000004")
        sim.register(near)
        sim.register(far)
        assert sim.get_pending_count() == 2

        # Wire OCO callback: first fill cancels sibling
        filled_ids = []

        def _oco_callback(event):
            filled_ids.append(event.order_id)
            if event.order_id == near.order_id:
                order_mgr.cancel(far.order_id, reason="oco_sibling_cancel", source="oco_bracket")
            elif event.order_id == far.order_id:
                order_mgr.cancel(near.order_id, reason="oco_sibling_cancel", source="oco_bracket")

        order_mgr.register_callback("on_fill", _oco_callback)

        # --- Poll 1: near tick arrives first ---
        def _make_tick(code, close):
            t = type("Tick", (), {})()
            t.code = code
            t.datetime = datetime.now()
            t.close = close
            t.open = close
            t.high = close
            t.low = close
            t.volume = 50
            return t

        sim.process_tick(_make_tick("TMF_NEAR", 47237))

        # Near should be FILLED, far should be CANCELLED
        assert near.status == OrderStatus.FILLED, \
            f"near should be FILLED, got {near.status}"
        assert far.status == OrderStatus.CANCELLED, \
            f"far should be CANCELLED by OCO, got {far.status}"
        assert len(filled_ids) == 1, \
            f"exactly one fill event expected, got {len(filled_ids)}: {filled_ids}"
        assert filled_ids[0] == near.order_id, \
            f"near should fill first, got {filled_ids[0]}"
        assert sim.get_pending_count() == 0, \
            f"both should be cleared from pending, got {sim.get_pending_count()}"

        # --- Poll 2: far tick arrives (should be no-op) ---
        sim.process_tick(_make_tick("TMF_FAR", 47473))

        # No additional fills — far was already cancelled
        assert len(filled_ids) == 1, \
            f"no second fill should occur after cancel, got {len(filled_ids)}"
        assert far.status == OrderStatus.CANCELLED, \
            f"far should stay CANCELLED, got {far.status}"
        assert sim.get_pending_count() == 0
