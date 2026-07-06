"""
Contract: OrderType.MKP (Market with Protection / 範圍市價單)

2026-06-08 JVS Claw: MKP is used for all MTS orders to avoid:
- MKT (市價): excessive slippage
- LMT (限價): unfilled orders due to price drift

MKP behavior:
- Paper mode: fills immediately at tick.close (same as MARKET)
- Live mode: Shioaji FuturesPriceType.MKP + OrderType.IOC (price=0)

Usage:
    pytest tests/contracts/test_order_type_mkp.py -v
"""
import time
from types import SimpleNamespace
from collections import deque
from unittest.mock import MagicMock

import pytest

from core.order_management.order import Order, OrderType, OrderSide, OrderStatus
from core.order_management.paper_fill import PaperFillSimulator
from core.order_management.order_manager import OrderManager


# ═══════════════════════════════════════════════════════════
# Test 1: MKP enum exists
# ═══════════════════════════════════════════════════════════

class TestMKPEnum:
    def test_mkp_enum_value(self):
        """OrderType.MKP must exist with value 'mkp'."""
        assert hasattr(OrderType, "MKP"), "OrderType must have MKP member"
        assert OrderType.MKP.value == "mkp"

    def test_mkp_distinct_from_market(self):
        """MKP and MARKET must be different enum values."""
        assert OrderType.MKP != OrderType.MARKET

    def test_create_order_with_mkp(self):
        """Can create an Order with order_type=MKP via OrderManager."""
        mgr = OrderManager(mode="paper")
        order = mgr.create_order(
            symbol="TMF_NEAR",
            side=OrderSide.BUY,
            order_type=OrderType.MKP,
            quantity=1,
            strategy="MTS_MANUAL",
        )
        assert order.order_type == OrderType.MKP
        assert order.status == OrderStatus.PENDING_SUBMIT


# ═══════════════════════════════════════════════════════════
# Test 2: PaperFillSimulator fills MKP at close
# ═══════════════════════════════════════════════════════════

class TestMKPPaperFill:
    def _make_fillable_order(self, mgr, side=OrderSide.BUY, order_type=OrderType.MKP):
        """Create and submit an order so it's in the manager's orders dict."""
        order = mgr.create_order(
            symbol="TMF_NEAR",
            side=side,
            order_type=order_type,
            quantity=1,
            strategy="TEST",
        )
        mgr.submit(order)
        return order

    def _make_tick(self, price=21000.0):
        return SimpleNamespace(
            code="TMF_NEAR",
            close=price,
            open=price,
            high=price + 5,
            low=price - 5,
            volume=100,
            datetime="2026-06-08 10:00:00",
        )

    def test_mkp_fills_at_close_buy(self):
        """MKP BUY order fills at tick.close price."""
        mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(mgr)
        mgr.set_simulator(sim)

        order = self._make_fillable_order(mgr, side=OrderSide.BUY)
        sim.register(order)

        sim.process_tick(self._make_tick(price=21000.0))

        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 1
        assert order.avg_fill_price == 21000.0

    def test_mkp_fills_at_close_sell(self):
        """MKP SELL order fills at tick.close price."""
        mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(mgr)
        mgr.set_simulator(sim)

        order = self._make_fillable_order(mgr, side=OrderSide.SELL)
        sim.register(order)

        sim.process_tick(self._make_tick(price=21050.0))

        assert order.status == OrderStatus.FILLED
        assert order.avg_fill_price == 21050.0

    def test_mkp_and_market_same_behavior(self):
        """MKP and MARKET produce identical fill results in paper mode."""
        mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(mgr)
        mgr.set_simulator(sim)

        mkt_order = self._make_fillable_order(mgr, order_type=OrderType.MARKET)
        mkp_order = self._make_fillable_order(mgr, order_type=OrderType.MKP)
        sim.register(mkt_order)
        sim.register(mkp_order)

        tick = self._make_tick(price=21000.0)
        sim.process_tick(tick)

        assert mkt_order.status == OrderStatus.FILLED
        assert mkp_order.status == OrderStatus.FILLED
        assert mkt_order.avg_fill_price == mkp_order.avg_fill_price


# ═══════════════════════════════════════════════════════════
# Test 3: OrderManager live submit maps MKP → price=0
# ═══════════════════════════════════════════════════════════

class TestMKPLiveMapping:
    def test_mkp_sets_price_to_zero_for_broker(self):
        """In live mode, MKP order must set price=0 before broker submission.
        ShioajiClient detects price==0 → FuturesPriceType.MKP."""
        mock_broker = MagicMock()
        mock_broker.place_order.return_value = SimpleNamespace(
            id="broker-123", seqno="001", ordno="001"
        )
        mgr = OrderManager(mode="live", broker_adapter=mock_broker)

        order = mgr.create_order(
            symbol="TMF_NEAR",
            side=OrderSide.BUY,
            order_type=OrderType.MKP,
            quantity=1,
            price=21000.0,  # User sets price but MKP should override to 0
            strategy="MTS_MANUAL",
        )
        mgr.submit(order)

        # After submit, price should be 0 (MKP ignores price)
        assert order.price == 0, "MKP must set price=0 for Shioaji broker"
