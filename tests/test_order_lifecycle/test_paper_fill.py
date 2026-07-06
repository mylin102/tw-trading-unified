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
