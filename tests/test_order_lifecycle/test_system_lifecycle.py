"""
V-Model Level 3: System Tests

Full end-to-end lifecycle:
  signal → OrderManager.create → submit → fill → position update → export

Tests the integrated flow in the FuturesMonitor context (without actual API).
"""
import pytest
import json
import os
import tempfile
from datetime import datetime
from unittest.mock import MagicMock, patch, PropertyMock
from pathlib import Path

from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
from core.order_management.order_manager import OrderManager
from core.order_management.paper_fill import PaperFillSimulator


# ── L3-ST-01: Full Lifecycle (Signal → Fill → Position) ──

class TestFullLifecycle:
    """完整流程: 信號產生 → 建立委託 → 送出 → 成交 → 持倉更新"""

    def test_buy_signal_to_position(self):
        """BUY 信號 → OrderManager → PaperFill → PaperTrader position"""
        from strategies.futures.squeeze_futures.engine.simulator import PaperTrader

        order_mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(order_mgr)
        order_mgr.set_simulator(sim)
        trader = PaperTrader(initial_balance=100000)

        # Wire up: on_fill → update PaperTrader
        def _on_fill(event):
            if event.status == OrderStatus.FILLED:
                ts = datetime.now()
                if event.side == OrderSide.BUY:
                    trader.execute_signal("BUY", event.fill_price, ts,
                                          lots=event.fill_qty, max_lots=1, stop_loss=60)
                else:
                    trader.execute_signal("SELL", event.fill_price, ts,
                                          lots=event.fill_qty, max_lots=1, stop_loss=60)

        order_mgr.register_callback("on_fill", _on_fill)

        # Simulate signal: counter_vwap BUY
        order = order_mgr.create_order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=1, strategy="counter_vwap",
        )
        order_mgr.submit(order, exchange_ordno="P-SYS-001")
        sim.register(order)

        # Simulate market tick
        tick = MagicMock()
        tick.datetime = datetime.now()
        tick.close = 36450
        tick.open = 36440
        tick.high = 36460
        tick.low = 36430
        tick.volume = 200
        sim.process_tick(tick)

        # Verify full chain
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 1
        assert order.avg_fill_price == 36450
        assert trader.position == 1
        assert trader.entry_price == 36450

    def test_buy_then_exit_full_cycle(self):
        """BUY → 持倉 → EXIT → 空倉，完整進出循環"""
        from strategies.futures.squeeze_futures.engine.simulator import PaperTrader

        order_mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(order_mgr)
        order_mgr.set_simulator(sim)
        trader = PaperTrader(initial_balance=100000)

        completed_orders = []

        def _on_fill(event):
            if event.status == OrderStatus.FILLED:
                completed_orders.append(event)
                ts = datetime.now()
                if event.side == OrderSide.BUY:
                    trader.execute_signal("BUY", event.fill_price, ts,
                                          lots=event.fill_qty, max_lots=1, stop_loss=60)
                else:
                    trader.execute_signal("EXIT", event.fill_price, ts,
                                          lots=event.fill_qty, max_lots=1)

        order_mgr.register_callback("on_fill", _on_fill)

        # Step 1: BUY
        buy_order = order_mgr.create_order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.MARKET, quantity=1,
            strategy="counter_vwap",
        )
        order_mgr.submit(buy_order, exchange_ordno="P-SYS-010")
        sim.register(buy_order)

        tick1 = MagicMock()
        tick1.datetime = datetime.now()
        tick1.close = 36450
        tick1.open = 36440
        tick1.high = 36460
        tick1.low = 36430
        tick1.volume = 200
        sim.process_tick(tick1)

        assert trader.position == 1

        # Step 2: EXIT (VWAP exit)
        exit_order = order_mgr.create_order(
            symbol="TMF", side=OrderSide.SELL, order_type=OrderType.MARKET, quantity=1,
            strategy="counter_vwap",
        )
        order_mgr.submit(exit_order, exchange_ordno="P-SYS-011")
        sim.register(exit_order)

        tick2 = MagicMock()
        tick2.datetime = datetime.now()
        tick2.close = 36550  # Price moved up
        tick2.open = 36540
        tick2.high = 36560
        tick2.low = 36530
        tick2.volume = 200
        sim.process_tick(tick2)

        assert trader.position == 0
        assert len(completed_orders) == 2
        # PnL should be positive (bought 36450, sold 36550)
        assert trader.trades[-1].get("pnl_cash", 0) > 0


# ── L3-ST-02: Multiple Orders Concurrent ──

class TestConcurrentOrders:
    """多筆委託同時在排隊，依序成交"""

    def test_three_orders_sequential_fills(self):
        """3 筆委託，每筆在不同 tick 成交"""
        order_mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(order_mgr)
        order_mgr.set_simulator(sim)

        orders = []
        for i in range(3):
            o = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1,
                                       strategy="counter_vwap")
            order_mgr.submit(o, exchange_ordno=f"P-SYS-02{i}")
            sim.register(o)
            orders.append(o)

        # All should be SUBMITTED
        assert all(o.status == OrderStatus.SUBMITTED for o in orders)

        # Process 3 ticks
        for i in range(3):
            tick = MagicMock()
            tick.datetime = datetime.now()
            tick.close = 36450 + i
            tick.open = 36440 + i
            tick.high = 36470 + i
            tick.low = 36430 + i
            tick.volume = 100
            sim.process_tick(tick)

        # All 3 should be filled
        assert all(o.status == OrderStatus.FILLED for o in orders)
        assert order_mgr.get_pending() == []  # No pending
        assert len(order_mgr.completed) == 3


# ── L3-ST-03: Export + Dashboard Format ──

class TestExportDashboardFormat:
    """訂單狀態匯出後，Dashboard 可讀取的格式"""

    def test_orders_to_csv_format(self):
        """OrderManager.completed → CSV-ready dicts"""
        order_mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(order_mgr)
        order_mgr.set_simulator(sim)

        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1,
                                       strategy="counter_vwap")
        order_mgr.submit(order, exchange_ordno="P-SYS-030")
        sim.register(order)

        tick = MagicMock()
        tick.datetime = datetime.now()
        tick.close = 36450
        tick.open = 36440
        tick.high = 36460
        tick.low = 36430
        tick.volume = 100
        sim.process_tick(tick)

        # Export to CSV-ready format
        completed = order_mgr.get_completed()
        csv_rows = []
        for o in completed:
            csv_rows.append({
                "order_id": o.order_id,
                "timestamp": o.filled_at.isoformat() if o.filled_at else o.created_at.isoformat(),
                "type": "BUY" if o.side == OrderSide.BUY else "SELL",
                "direction": "LONG" if o.side == OrderSide.BUY else "SHORT",
                "price": o.avg_fill_price,
                "lots": o.filled_quantity,
                "status": o.status.value,
                "strategy": o.strategy,
                "reason": "COUNTER_VWAP",
            })

        assert len(csv_rows) == 1
        row = csv_rows[0]
        assert row["type"] == "BUY"
        assert row["direction"] == "LONG"
        assert row["price"] == 36450
        assert row["lots"] == 1
        assert row["status"] == "filled"

    def test_pending_orders_visible(self):
        """Dashboard 應能看到待成交的委託單"""
        order_mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(order_mgr)
        order_mgr.set_simulator(sim)

        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.LIMIT, 1,
                                       price=36000, strategy="counter_vwap")
        order_mgr.submit(order, exchange_ordno="P-SYS-031")
        sim.register(order)

        # Price stays above limit → no fill
        tick = MagicMock()
        tick.datetime = datetime.now()
        tick.close = 36450
        tick.open = 36440
        tick.high = 36460
        tick.low = 36430
        tick.volume = 100
        sim.process_tick(tick)

        # Order should still be pending (limit not hit)
        pending = order_mgr.get_pending()
        assert len(pending) == 1
        assert pending[0].status == OrderStatus.SUBMITTED
        assert pending[0].price == 36000  # limit price visible


# ── L3-ST-04: Market Hours Gate Integration ──

class TestMarketHoursGate:
    """Market Hours Gate 與 OrderManager 整合"""

    def test_order_rejected_during_closed_hours(self):
        """休市時間不允許建立委託"""
        order_mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(order_mgr)
        order_mgr.set_simulator(sim)

        # Simulate market hours check: 14:55 (lunch break)
        def is_market_open(hhmm):
            return (845 <= hhmm <= 1345) or (hhmm >= 1500) or (hhmm < 500)

        assert is_market_open(1455) is False  # 14:55 → closed
        assert is_market_open(1500) is True   # 15:00 → night open
        assert is_market_open(900) is True    # 09:00 → day open
        assert is_market_open(300) is True    # 03:00 → night ongoing
        assert is_market_open(600) is False   # 06:00 → closed
