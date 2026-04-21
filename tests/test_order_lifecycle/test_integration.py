"""
V-Model Level 2: Integration Tests

Tests OrderManager + PaperTrader integration, Shioaji callback flow,
and restart recovery.
"""
import pytest
import json
from datetime import datetime
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
from core.order_management.order_manager import OrderManager
from core.order_management.paper_fill import PaperFillSimulator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OPTIONS_ROOT = PROJECT_ROOT / "strategies" / "options"
OPTIONS_SRC = OPTIONS_ROOT / "src"
for path in (str(OPTIONS_ROOT), str(OPTIONS_SRC)):
    if path not in sys.path:
        sys.path.insert(0, path)

from strategies.options import live_options_squeeze_monitor as options_module


def _build_live_combo_monitor():
    monitor = options_module.ShioajiOptionsSmartMonitor.__new__(options_module.ShioajiOptionsSmartMonitor)
    monitor.mode = "live"
    monitor.live_trading = True
    monitor.order_mgr = OrderManager(mode="live")
    monitor.position = 0
    monitor.active_side = None
    monitor.entry_price = 0.0
    monitor.entry_time = None
    monitor.stop_loss_pct = 0.1
    monitor.stop_loss_price = 0.0
    monitor.peak_premium = 0.0
    monitor.has_tp1_hit = False
    monitor.pending_entry = None
    monitor.pending_exit_qty = 0
    monitor.pending_exit_reason = None
    monitor.pending_exit_trade = None
    monitor.pending_theta_combo = None
    monitor.cooldown_bars = 0
    monitor.cooldown_until = 0
    monitor._theta_cfg = {"enabled": True}
    monitor._theta_bars_held = 0
    monitor._theta_release_confirm_count = 0
    monitor._theta_release_last_bar_ts = None
    monitor._seen_fill_ordnos = set()
    monitor._seen_fill_identities = set()
    monitor.sync_contract_quotes = lambda: None
    monitor._audit_signal = lambda *args, **kwargs: None
    monitor.log_trade_events = []
    monitor.log_trade = lambda *args, **kwargs: monitor.log_trade_events.append((args, kwargs))
    monitor._save_orders_file_wrapper = lambda: None
    monitor.api = SimpleNamespace(
        futopt_account="ACC",
        margin=lambda account: SimpleNamespace(equity=30000, order_margin_premium=50),
    )
    monitor._theta_gang = SimpleNamespace(position=None, strategy="bull_put_spread")
    monitor.broker = None
    return monitor


# ── L2-IT-01: OrderManager + PaperTrader Integration ──

class TestOrderManagerPaperTrader:
    """OrderManager 驅動 PaperTrader 的整合測試"""

    def test_order_fill_updates_position(self):
        """Order 成交後，PaperTrader position 應同步更新"""
        from strategies.futures.squeeze_futures.engine.simulator import PaperTrader

        order_mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(order_mgr)
        order_mgr.set_simulator(sim)

        trader = PaperTrader(initial_balance=100000)

        # Register callback: on_fill → update trader position
        def _on_fill_callback(event):
            if event.status == OrderStatus.FILLED:
                ts = datetime.now()
                if event.side == OrderSide.BUY:
                    trader.execute_signal("BUY", event.fill_price, ts,
                                          lots=event.fill_qty, max_lots=1, stop_loss=60)
                else:
                    trader.execute_signal("SELL", event.fill_price, ts,
                                          lots=event.fill_qty, max_lots=1, stop_loss=60)

        order_mgr.register_callback("on_fill", _on_fill_callback)

        # Create and fill order
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        order_mgr.submit(order, exchange_ordno="P-100")
        sim.register(order)

        # Simulate tick
        tick = MagicMock()
        tick.datetime = datetime.now()
        tick.close = 36450
        tick.open = 36440
        tick.high = 36460
        tick.low = 36430
        tick.volume = 100
        sim.process_tick(tick)

        # Verify both order and trader updated
        assert order.status == OrderStatus.FILLED
        assert trader.position == 1
        assert trader.entry_price == 36450

    def test_multiple_orders_sequence(self):
        """多筆委託依序成交，PaperTrader 正確跟隨"""
        order_mgr = OrderManager(mode="paper")
        sim = PaperFillSimulator(order_mgr)
        order_mgr.set_simulator(sim)

        fills = []

        def _on_fill(event):
            fills.append({
                "order_id": event.order_id,
                "price": event.fill_price,
                "qty": event.fill_qty,
            })

        order_mgr.register_callback("on_fill", _on_fill)

        # Create 3 orders
        for i in range(3):
            order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
            order_mgr.submit(order, exchange_ordno=f"P-20{i}")
            sim.register(order)

        # Process ticks
        tick = MagicMock()
        tick.open = 36400
        tick.high = 36450
        tick.low = 36390
        tick.close = 36440
        tick.volume = 300

        tick.datetime = datetime.now()
        sim.process_tick(tick)
        tick.datetime = datetime.now()
        sim.process_tick(tick)
        tick.datetime = datetime.now()
        sim.process_tick(tick)

        assert len(fills) == 3
        assert all(f["price"] == 36440 for f in fills)


# ── L2-IT-02: Restart Recovery ──

class TestRestartRecovery:
    """重啟後從 API 重建訂單狀態"""

    def test_rebuild_filled_and_open(self):
        """api.list_trades() + api.list_open_orders() 重建狀態"""
        order_mgr = OrderManager(mode="live", broker_adapter=MagicMock())

        # Mock API responses
        filled_trade = MagicMock()
        filled_trade.ordno = "EXCH-100"
        filled_trade.price = 36450
        filled_trade.quantity = 1
        filled_trade.action = "Buy"
        filled_trade.symbol = "TMF"

        open_order = MagicMock()
        open_order.ordno = "EXCH-200"
        open_order.price = 36500
        open_order.quantity = 1
        open_order.action = "Sell"
        open_order.symbol = "TMF"

        result = order_mgr.recover_from_api(
            filled_trades=[filled_trade],
            open_orders=[open_order],
        )

        assert result["filled"] == 1
        assert result["open"] == 1
        assert len(order_mgr.completed) == 1
        assert order_mgr.completed[0].status == OrderStatus.FILLED
        assert len(order_mgr.get_pending()) == 1
        assert order_mgr.get_pending()[0].status == OrderStatus.SUBMITTED

    def test_rebuild_empty_api(self):
        """API 無資料時應正常處理"""
        order_mgr = OrderManager(mode="live", broker_adapter=MagicMock())
        result = order_mgr.recover_from_api(filled_trades=[], open_orders=[])
        assert result["filled"] == 0
        assert result["open"] == 0
        assert len(order_mgr.completed) == 0
        assert len(order_mgr.get_pending()) == 0


# ── L2-IT-03: Shioaji Callback Flow ──

class TestShioajiCallback:
    """Shioaji order callback → OrderManager 的整合"""

    def test_callback_partial_then_full(self):
        """Shioaji callback: 部分成交 → 完全成交"""
        order_mgr = OrderManager(mode="live", broker_adapter=MagicMock())

        # Manually create a submitted order (simulating pre-crash state)
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 3)
        order.status = OrderStatus.SUBMITTED
        order.exchange_order_id = "SHIOAJI-001"

        # Callback: partial fill
        order_mgr.on_fill(order.order_id, fill_price=36450, fill_qty=1, partial=True)
        assert order.status == OrderStatus.PARTIAL_FILLED
        assert order.filled_quantity == 1

        # Callback: remaining filled
        order_mgr.on_fill(order.order_id, fill_price=36460, fill_qty=2, partial=False)
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 3
        assert order.order_id not in order_mgr.active_orders
        assert order in order_mgr.completed

    def test_callback_reject(self):
        """Shioaji callback: 委託被拒"""
        order_mgr = OrderManager(mode="live", broker_adapter=MagicMock())
        order = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        order_mgr.submit(order, exchange_ordno="SHIOAJI-002")

        order_mgr.reject(order.order_id, "insufficient_margin")
        assert order.status == OrderStatus.REJECTED
        assert order.order_id not in order_mgr.active_orders


# ── L2-IT-04: Export Format ──

class TestExportFormat:
    """訂單狀態匯出格式驗證"""

    def test_order_to_dict_complete(self):
        """Order.to_dict() 應包含所有必要欄位"""
        order = Order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=2, strategy="counter_vwap",
        )
        order.submit("EXCH-001")
        order.fill(36450, 1)
        order.fill(36460, 1)

        d = order.to_dict()

        required_keys = [
            "order_id", "symbol", "side", "order_type", "quantity",
            "filled_quantity", "remaining_quantity", "price",
            "avg_fill_price", "status", "strategy",
            "commission", "tax", "total_fee", "slippage",
            "exchange_order_id", "created_at", "submitted_at", "filled_at",
        ]
        for key in required_keys:
            assert key in d, f"Missing key: {key}"

        assert d["status"] == "filled"
        assert d["filled_quantity"] == 2
        assert d["remaining_quantity"] == 0

    def test_order_json_serializable(self):
        """Order.to_json() 應可被 json.loads 解析"""
        order = Order(
            symbol="TMF", side=OrderSide.SELL, order_type=OrderType.LIMIT,
            quantity=1, price=36500, strategy="spring_upthrust",
        )
        order.submit("EXCH-002")
        order.fill(36510, 1)

        j = order.to_json()
        parsed = json.loads(j)
        assert parsed["status"] == "filled"
        assert parsed["avg_fill_price"] == 36510


class TestComboStartupRecovery:
    def test_combo_startup_recovery_checks_broker_before_ledger_fallback(self):
        monitor = options_module.ShioajiOptionsSmartMonitor.__new__(options_module.ShioajiOptionsSmartMonitor)
        monitor.live_trading = True
        calls = []
        monitor._recover_live_orders_from_broker = lambda: calls.append("broker") or {"filled": 0, "open": 0, "failed": 0}
        monitor._recover_orders_from_ledger = lambda: calls.append("ledger")

        recovered = monitor._startup_recover_live_order_state()

        assert recovered == {"filled": 0, "open": 0, "failed": 0}
        assert calls == ["broker", "ledger"]

    def test_combo_startup_recovery_restores_pending_combo_without_paper_fallback(self):
        monitor = _build_live_combo_monitor()
        monitor._recover_orders_from_ledger = MagicMock()
        monitor.broker = SimpleNamespace(
            list_combo_status_trades=lambda account=None: [
                SimpleNamespace(
                    id="BROKER-COMBO-START-001",
                    seqno="SEQ-COMBO-START-001",
                    ordno="ORDNO-COMBO-START-001",
                    action="Sell",
                    quantity=2,
                    strategy="bull_put_spread",
                    price=48.0,
                    status=SimpleNamespace(status="Submitted", quantity=2, price=48.0, deals={}),
                )
            ],
            list_open_orders=lambda account=None: [],
            list_trades=lambda account=None: [],
        )

        recovered = monitor._startup_recover_live_order_state()
        pending_order = monitor.order_mgr.get_pending()[0]

        assert recovered == {"filled": 0, "open": 1, "failed": 0}
        assert monitor._recover_orders_from_ledger.call_count == 0
        assert pending_order.symbol == "TXO-COMBO"
        assert pending_order.truth_source == "broker_combo"
        assert monitor.pending_theta_combo["phase"] == "entry"
        assert monitor.pending_theta_combo["order_id"] == pending_order.order_id
        assert monitor.position == 0
