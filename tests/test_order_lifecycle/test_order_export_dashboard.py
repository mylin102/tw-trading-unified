"""
V-Model L3 Extension: Order Export → Dashboard Integration Tests

Tests that OrderManager orders are exported to files in a format
the dashboard can read and display.
"""
import pytest
import json
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
from core.order_management.order_manager import OrderManager


# ── L3-ORD-01: Order Export Format ──

class TestOrderExportFormat:
    """訂單匯出格式應包含 Dashboard 需要的所有欄位"""

    def test_order_to_dict_has_all_dashboard_fields(self):
        """Order.to_dict() 應包含委託單狀態、類型、成交細節"""
        order = Order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity=2, price=36400, strategy="counter_vwap",
            comment="VWAP counter entry",
        )
        order.submit("EXCH-001")
        order.fill(36390, 1)  # Partial fill at better price
        order.fill(36395, 1)  # Full fill

        d = order.to_dict()

        # Dashboard 必須看到的欄位
        assert "order_id" in d
        assert "symbol" in d
        assert "side" in d          # buy/sell
        assert "order_type" in d    # market/limit/stop/stop_limit
        assert "quantity" in d      # 委託總數量
        assert "filled_quantity" in d  # 已成交數量
        assert "remaining_quantity" in d  # 剩餘數量
        assert "price" in d         # 限價價格
        assert "avg_fill_price" in d  # 平均成交價
        assert "status" in d        # submitted/partial_filled/filled/cancelled/rejected
        assert "strategy" in d      # 策略名稱
        assert "comment" in d       # 備註
        assert "commission" in d    # 手續費
        assert "tax" in d           # 稅
        assert "total_fee" in d     # 總摩擦成本
        assert "slippage" in d      # 滑價
        assert "exchange_order_id" in d  # 交易所委託編號
        assert "intent_id" in d
        assert "broker_order_id" in d
        assert "seqno" in d
        assert "ordno" in d
        assert "fills" in d
        assert "created_at" in d    # 建立時間
        assert "submitted_at" in d  # 送出時間
        assert "filled_at" in d     # 成交時間

        assert d["intent_id"].startswith("intent_")
        assert d["broker_order_id"] == "EXCH-001"
        assert isinstance(d["fills"], list)
        assert d["fills"][0]["deal_id"].startswith("deal_")

    def test_market_order_export(self):
        """市價單匯出格式"""
        order = Order(
            symbol="TMF", side=OrderSide.SELL, order_type=OrderType.MARKET,
            quantity=1, strategy="counter_vwap",
        )
        order.submit("EXCH-002")
        order.fill(36500, 1)

        d = order.to_dict()
        assert d["order_type"] == "market"
        assert d["price"] is None  # 市價單無限價
        assert d["avg_fill_price"] == 36500
        assert d["status"] == "filled"

    def test_partial_fill_export(self):
        """部分成交匯出格式"""
        order = Order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=5, strategy="counter_vwap",
        )
        order.submit("EXCH-003")
        order.fill(36450, 2)

        d = order.to_dict()
        assert d["status"] == "partial_filled"
        assert d["quantity"] == 5
        assert d["filled_quantity"] == 2
        assert d["remaining_quantity"] == 3

    def test_cancelled_order_export(self):
        """已取消委託單匯出格式"""
        order = Order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.LIMIT,
            quantity=1, price=36000, strategy="counter_vwap",
        )
        order.submit("EXCH-004")
        order.cancel(reason="user_request")

        d = order.to_dict()
        assert d["status"] == "cancelled"
        assert d["cancel_reason"] == "user_request"

    def test_rejected_order_export(self):
        """被退單匯出格式"""
        order = Order(
            symbol="TMF", side=OrderSide.BUY, order_type=OrderType.MARKET,
            quantity=1, strategy="counter_vwap",
        )
        order.reject(reason="insufficient_margin")

        d = order.to_dict()
        assert d["status"] == "rejected"
        assert d["reject_reason"] == "insufficient_margin"


# ── L3-ORD-02: Orders File Export ──

class TestOrdersFileExport:
    """OrderManager 應能匯出 orders.json 供 Dashboard 讀取"""

    def test_export_orders_to_file(self):
        """OrderManager.get_completed() + get_pending() → JSON"""
        order_mgr = OrderManager(mode="paper")

        # Create some orders
        o1 = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1,
                                     strategy="counter_vwap", comment="test entry")
        order_mgr.submit(o1, exchange_ordno="P-100")
        order_mgr.on_fill(o1.order_id, 36450, 1, partial=False)

        o2 = order_mgr.create_order("TMF", OrderSide.SELL, OrderType.LIMIT, 1, price=36600,
                                     strategy="counter_vwap", comment="test exit")
        order_mgr.submit(o2, exchange_ordno="P-101")

        # Export format
        all_orders = order_mgr.get_completed() + order_mgr.get_pending()
        export_data = [o.to_dict() for o in all_orders]

        assert len(export_data) == 2
        # Verify serializable
        json_str = json.dumps(export_data, ensure_ascii=False, indent=2)
        parsed = json.loads(json_str)
        assert len(parsed) == 2

    def test_dashboard_can_read_orders_file(self):
        """Dashboard 讀取 orders.json 並顯示表格"""
        order_mgr = OrderManager(mode="paper")

        # Create and fill an order through OrderManager
        o = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1,
                                    strategy="counter_vwap")
        order_mgr.submit(o, exchange_ordno="P-200")
        order_mgr.on_fill(o.order_id, 36450, 1, partial=False)

        # Export
        export_data = [order.to_dict() for order in order_mgr.get_completed()]

        # Simulate dashboard reading
        import pandas as pd
        df = pd.DataFrame(export_data)

        assert "order_id" in df.columns
        assert "status" in df.columns
        assert "avg_fill_price" in df.columns
        assert df.iloc[0]["status"] == "filled"
        assert df.iloc[0]["avg_fill_price"] == 36450

    def test_pending_orders_visible_in_dashboard(self):
        """Dashboard 應能看到排隊中的委託單"""
        order_mgr = OrderManager(mode="paper")

        o = order_mgr.create_order("TMF", OrderSide.BUY, OrderType.LIMIT, 1, price=36000,
                                    strategy="counter_vwap")
        order_mgr.submit(o, exchange_ordno="P-300")
        # Not filled → still pending

        pending = order_mgr.get_pending()
        assert len(pending) == 1
        assert pending[0].status == OrderStatus.SUBMITTED

        export_data = [o.to_dict() for o in pending]
        assert export_data[0]["order_type"] == "limit"
        assert export_data[0]["price"] == 36000

class TestExportJsonSafe:
    """[export fix] Shioaji enum / non-serializable values never crash
    the orders export; order identity and dedupe keys are preserved."""

    def test_order_to_dict_action_datetime_does_not_crash(self):
        """A leaked Action where a datetime belongs serializes as None
        instead of crashing on .isoformat()."""
        from enum import Enum

        class _Action(Enum):
            SELL = "sell"

        o = Order(symbol="TMFH6", side=OrderSide.SELL,
                  order_type=OrderType.MARKET, quantity=1, price=0.0,
                  strategy="MTS_ENTRY", account="futures",
                  order_id="ORD-A1")
        o.status = OrderStatus.SUBMITTED
        o.created_at = _Action.SELL
        o.filled_at = _Action.SELL
        d = o.to_dict()
        assert d["created_at"] is None
        assert d["filled_at"] is None
        assert d["order_id"] == "ORD-A1"
        assert d["symbol"] == "TMFH6"

    def test_save_orders_wrapper_persists_action_laden_records(
            self, tmp_path, monkeypatch):
        """The export wrapper normalizes enum-laden order records
        JSON-safe and persists them without crashing; identity and
        dedupe keys are unchanged."""
        from enum import Enum
        from strategies.futures.monitor import FuturesMonitor

        class _Action(Enum):
            SELL = "sell"

        m = FuturesMonitor.__new__(FuturesMonitor)
        om = OrderManager(mode="paper")
        o = om.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1,
                            strategy="MTS_ENTRY", account="futures")
        om.submit(o, exchange_ordno="P-1")
        o.account = _Action.SELL          # leaked enum in a string field
        m.order_mgr = om
        m.trader = MagicMock(position=0)
        m._registry = {}
        m.ticker = "TMF"
        m._session_date = "20260813"
        (tmp_path / "exports" / "trades").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        # the wrapper writes under runtime_path("exports", "trades") —
        # redirect the runtime dir to the tmp tree so the real runtime
        # exports are never touched.
        monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))

        m._save_orders_file_wrapper()

        # the wrapper derives the filename from the order manager's
        # session date (not the monitor attribute)
        f = tmp_path / "exports" / "trades" / \
            f"TMF_{om._session_date}_orders.json"
        assert f.exists()
        data = json.loads(f.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["order_id"] == o.order_id
        assert isinstance(data[0]["account"], str)

