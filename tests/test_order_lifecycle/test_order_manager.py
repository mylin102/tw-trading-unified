"""
V-Model Level 1: Unit Tests for OrderManager

Tests the unified order manager for both paper and live modes.
Covers: create, submit, fill (partial+full), cancel gate, reject, expire, recovery.
"""
import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch
from types import SimpleNamespace

from core.order_management.order import Order, OrderStatus, OrderType, OrderSide
from core.order_management.order_manager import OrderManager, OrderEvent


# ── Fixtures ──

@pytest.fixture
def paper_mgr():
    """Paper mode OrderManager (no broker)"""
    return OrderManager(mode="paper")


@pytest.fixture
def live_mgr():
    """Live mode OrderManager (mock broker)"""
    broker = MagicMock()
    # 2026-06-23 Gemini CLI: Remove place_order_object to prevent MagicMock hasattr resolving True
    del broker.place_order_object
    return OrderManager(mode="live", broker_adapter=broker)


# ── L1-UT-01: Create Order ──

class TestCreateOrder:
    def test_create_buy_market(self, paper_mgr):
        order = paper_mgr.create_order(
            symbol="TMF", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=1,
        )
        assert order.order_id in paper_mgr.active_orders
        assert order.status == OrderStatus.PENDING_SUBMIT
        assert order.side == OrderSide.BUY
        assert order.quantity == 1
        assert order.filled_quantity == 0

    def test_create_sell_limit(self, paper_mgr):
        order = paper_mgr.create_order(
            symbol="TMF", side=OrderSide.SELL,
            order_type=OrderType.LIMIT, quantity=2, price=36500,
        )
        assert order.order_type == OrderType.LIMIT
        assert order.price == 36500
        assert order.status == OrderStatus.PENDING_SUBMIT

    def test_create_generates_unique_ids(self, paper_mgr):
        o1 = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        o2 = paper_mgr.create_order("TMF", OrderSide.SELL, OrderType.MARKET, 1)
        assert o1.order_id != o2.order_id

    def test_create_tracks_symbol(self, paper_mgr):
        order = paper_mgr.create_order(
            symbol="TXO", side=OrderSide.BUY,
            order_type=OrderType.MARKET, quantity=1,
            strategy="counter_vwap",
        )
        assert order.symbol == "TXO"
        assert order.strategy == "counter_vwap"

    def test_create_accepts_combo_truth_metadata(self, paper_mgr):
        order = paper_mgr.create_order(
            symbol="TXO-SPREAD",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=1,
            price=15.5,
            truth_source="broker_combo",
            combo_strategy="vertical_spread",
            combo_legs=[
                {"code": "TXO22000C", "action": "Sell", "ratio": 1},
                {"code": "TXO22100C", "action": "Buy", "ratio": 1},
            ],
        )
        assert order.truth_source == "broker_combo"
        assert order.combo_strategy == "vertical_spread"
        assert len(order.combo_legs) == 2


# ── L1-UT-02: Submit Order ──

class TestSubmit:
    def test_submit_paper_sets_submitted(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        ok = paper_mgr.submit(order, exchange_ordno="EXCH-001")
        assert ok is True
        assert order.status == OrderStatus.SUBMITTED
        assert order.exchange_order_id == "EXCH-001"
        assert order.submitted_at is not None

    def test_submit_live_calls_broker(self, live_mgr):
        order = live_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        live_mgr.broker_adapter.place_order.return_value = MagicMock(ordno="EXCH-002")
        ok = live_mgr.submit(order)
        assert ok is True
        live_mgr.broker_adapter.place_order.assert_called_once()
        assert order.status == OrderStatus.SUBMITTED
        assert order.exchange_order_id == "EXCH-002"

    def test_submit_live_failure(self, live_mgr):
        order = live_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        live_mgr.broker_adapter.place_order.return_value = None
        ok = live_mgr.submit(order)
        assert ok is False
        assert order.status == OrderStatus.REJECTED

    def test_submit_already_submitted_raises(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        with pytest.raises(ValueError, match="already submitted"):
            paper_mgr.submit(order, exchange_ordno="EXCH-002")

    def test_attach_submission_preserves_broker_ids(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        attached = paper_mgr.attach_submission(
            order.order_id,
            broker_order_id="BROKER-001",
            seqno="SEQ-001",
            ordno="ORDNO-001",
            raw_status="Submitted",
        )
        assert attached.broker_order_id == "BROKER-001"
        assert attached.seqno == "SEQ-001"
        assert attached.ordno == "ORDNO-001"
        assert attached.status == OrderStatus.SUBMITTED

    def test_attach_submission_persists_combo_payload_on_single_lifecycle_order(self, paper_mgr):
        order = paper_mgr.create_order(
            "TXO-SPREAD",
            OrderSide.SELL,
            OrderType.LIMIT,
            1,
            price=16.0,
            truth_source="broker_combo",
            combo_strategy="vertical_spread",
            combo_legs=[
                {"code": "TXO22000C", "action": "Sell", "ratio": 1},
                {"code": "TXO22100C", "action": "Buy", "ratio": 1},
            ],
        )
        broker_trade = SimpleNamespace(
            id="BROKER-COMBO-001",
            seqno="SEQ-COMBO-001",
            ordno="ORDNO-COMBO-001",
            combo_id="COMBO-001",
            legs=[{"code": "TXO22000C"}, {"code": "TXO22100C"}],
        )

        attached = paper_mgr.attach_submission(
            order.order_id,
            broker_trade=broker_trade,
            raw_status="Submitted",
            source="broker_combo_submit",
        )

        assert attached.order_id == order.order_id
        assert len(paper_mgr.active_orders) == 1
        assert attached.broker_order_id == "BROKER-COMBO-001"
        assert attached.seqno == "SEQ-COMBO-001"
        assert attached.ordno == "ORDNO-COMBO-001"
        assert attached.raw_events[-1]["payload"]["combo_id"] == "COMBO-001"
        assert len(attached.combo_legs) == 2


# ── L1-UT-03: Fill Order (Full) ──

class TestFill:
    def test_full_fill_market(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(order.order_id, fill_price=36450, fill_qty=1, partial=False)
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 1
        assert order.avg_fill_price == 36450
        assert order.order_id not in paper_mgr.active_orders
        assert order in paper_mgr.completed

    def test_fill_moves_order_to_completed(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(order.order_id, 36450, 1, partial=False)
        assert len(paper_mgr.active_orders) == 0
        assert len(paper_mgr.completed) == 1


# ── L1-UT-04: Fill Order (Partial) ──

class TestPartialFill:
    def test_partial_fill(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 3)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(order.order_id, fill_price=36450, fill_qty=1, partial=True)
        assert order.status == OrderStatus.PARTIAL_FILLED
        assert order.filled_quantity == 1
        assert order.get_remaining_quantity() == 2
        assert order.order_id in paper_mgr.active_orders

    def test_partial_then_complete(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 2)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(order.order_id, 36450, 1, partial=True)
        assert order.status == OrderStatus.PARTIAL_FILLED
        paper_mgr.on_fill(order.order_id, 36460, 1, partial=False)
        assert order.status == OrderStatus.FILLED
        assert order.filled_quantity == 2
        # Average of two fills
        assert order.avg_fill_price == pytest.approx(36455.0)

    def test_partial_fill_avg_price(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 5)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(order.order_id, 36400, 2, partial=True)
        paper_mgr.on_fill(order.order_id, 36500, 3, partial=False)
        assert order.avg_fill_price == pytest.approx(36460.0)  # (36400*2 + 36500*3) / 5


# ── L1-UT-05: Cancel Gate (🛑 Key Rule) ──

class TestCancelGate:
    def test_cancel_submitted_ok(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.cancel(order.order_id, reason="user_request")
        assert order.status == OrderStatus.CANCELLED
        assert order.cancel_reason == "user_request"
        assert order in paper_mgr.completed

    def test_cancel_partial_filled_ok(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 3)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(order.order_id, 36450, 1, partial=True)
        paper_mgr.cancel(order.order_id, reason="risk_limit")
        assert order.status == OrderStatus.CANCELLED

    def test_cancel_filled_rejected(self, paper_mgr):
        """🛑 Filled orders CANNOT be cancelled"""
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(order.order_id, 36450, 1, partial=False)
        with pytest.raises(ValueError, match="terminal"):
            paper_mgr.cancel(order.order_id, reason="user_request")

    def test_cancel_already_cancelled_rejected(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.cancel(order.order_id, reason="first")
        with pytest.raises(ValueError, match="terminal"):
            paper_mgr.cancel(order.order_id, reason="second")

    def test_cancel_rejected_rejected(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.reject(order.order_id, "margin_exceeded")
        with pytest.raises(ValueError, match="terminal"):
            paper_mgr.cancel(order.order_id)

    def test_cancel_expired_rejected(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.expire(order.order_id)
        with pytest.raises(ValueError, match="terminal"):
            paper_mgr.cancel(order.order_id)

    def test_live_cancel_calls_broker(self):
        broker = MagicMock()
        # 2026-06-23 Gemini CLI: Remove place_order_object to prevent MagicMock hasattr resolving True
        del broker.place_order_object
        mgr = OrderManager(mode="live", broker_adapter=broker)
        order = mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        broker.place_order.return_value = MagicMock(ordno="EXCH-001")
        mgr.submit(order)
        broker.cancel_order.return_value = True
        mgr.cancel(order.order_id, reason="user")
        broker.cancel_order.assert_called_once_with("EXCH-001")


# ── L1-UT-06: Reject & Expire ──

class TestRejectExpire:
    def test_reject(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.reject(order.order_id, "insufficient_margin")
        assert order.status == OrderStatus.REJECTED
        assert order.reject_reason == "insufficient_margin"

    def test_reject_after_submit(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.reject(order.order_id, "exchange_reject")
        assert order.status == OrderStatus.REJECTED

    def test_expire(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.expire(order.order_id)
        assert order.status == OrderStatus.EXPIRED
        assert order.expired_at is not None

    def test_reject_filled_rejected(self, paper_mgr):
        """🛑 Terminal states cannot be rejected"""
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(order.order_id, 36450, 1, partial=False)
        with pytest.raises(ValueError, match="terminal"):
            paper_mgr.reject(order.order_id, "test")


# ── L1-UT-07: Query Methods ──

class TestQuery:
    def test_get_pending_returns_active(self, paper_mgr):
        o1 = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(o1, exchange_ordno="EXCH-001")
        o2 = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(o2, exchange_ordno="EXCH-002")
        paper_mgr.on_fill(o1.order_id, 36450, 1, partial=False)
        pending = paper_mgr.get_pending()
        assert len(pending) == 1
        assert pending[0].order_id == o2.order_id

    def test_get_pending_empty(self, paper_mgr):
        assert len(paper_mgr.get_pending()) == 0

    def test_get_completed(self, paper_mgr):
        o1 = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(o1, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(o1.order_id, 36450, 1, partial=False)
        completed = paper_mgr.get_completed()
        assert len(completed) == 1
        assert completed[0].order_id == o1.order_id

    def test_get_orders_by_symbol(self, paper_mgr):
        paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.create_order("TXO", OrderSide.BUY, OrderType.MARKET, 1)
        tmf_orders = paper_mgr.get_orders_by_symbol("TMF")
        assert len(tmf_orders) == 1
        assert tmf_orders[0].symbol == "TMF"


# ── L1-UT-08: Recovery (Restart) ──

class TestRecovery:
    def test_rebuild_from_api(self, live_mgr):
        """重啟後應立即呼叫 api.list_trades() 重建狀態"""
        # Mock API returns: 1 filled trade, 1 pending order
        filled_trade = MagicMock()
        filled_trade.ordno = "EXCH-100"
        filled_trade.price = 36450
        filled_trade.quantity = 1
        filled_trade.action = "Buy"

        open_order = MagicMock()
        open_order.ordno = "EXCH-200"
        open_order.price = 36500
        open_order.quantity = 1
        open_order.action = "Sell"

        live_mgr.broker_adapter.list_trades.return_value = [filled_trade]
        live_mgr.broker_adapter.list_open_orders.return_value = [open_order]

        # Pre-register orders that were in-flight at crash
        live_mgr.recover_from_api(
            filled_trades=[filled_trade],
            open_orders=[open_order],
        )

        # Filled trade should create a completed order
        assert len(live_mgr.completed) == 1
        completed = live_mgr.completed[0]
        assert completed.status == OrderStatus.FILLED
        assert completed.exchange_order_id == "EXCH-100"

        # Open order should be in active
        assert len(live_mgr.get_pending()) == 1
        pending = live_mgr.get_pending()[0]
        assert pending.status == OrderStatus.SUBMITTED
        assert pending.exchange_order_id == "EXCH-200"


class TestComboSerialization:
    def test_order_round_trip_preserves_truth_source_and_combo_metadata(self, paper_mgr):
        order = paper_mgr.create_order(
            symbol="TXO-SPREAD",
            side=OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=1,
            price=15.5,
            strategy="theta_gang",
            comment="combo order",
            truth_source="broker_combo",
            combo_strategy="bull_put_spread",
            combo_legs=[
                {"code": "TXO21900P", "action": "Sell", "ratio": 1},
                {"code": "TXO21800P", "action": "Buy", "ratio": 1},
            ],
        )

        paper_mgr.attach_submission(
            order.order_id,
            broker_trade=SimpleNamespace(id="BROKER-123", seqno="SEQ-123", ordno="ORDNO-123"),
            raw_status="Submitted",
            source="broker_combo_submit",
        )
        paper_mgr.apply_deal_fill(
            order.order_id,
            deal_id="DEAL-123",
            fill_price=15.0,
            fill_qty=1,
            broker_order_id="BROKER-123",
            ordno="ORDNO-123",
            source="broker_combo_fill",
            reason="combo_fill",
            raw_payload={"legs": [{"code": "TXO21900P"}, {"code": "TXO21800P"}]},
        )

        exported = order.to_dict()
        restored = Order.from_dict(exported)

        assert exported["truth_source"] == "broker_combo"
        assert exported["combo_strategy"] == "bull_put_spread"
        assert exported["combo_legs"][0]["code"] == "TXO21900P"
        assert restored.truth_source == "broker_combo"
        assert restored.combo_strategy == "bull_put_spread"
        assert restored.combo_legs[1]["action"] == "Buy"
        assert restored.status == OrderStatus.FILLED
        assert restored.avg_fill_price == 15.0
        assert restored.broker_order_id == "BROKER-123"
        assert restored.ordno == "ORDNO-123"
        assert restored.raw_events[-1]["payload"]["legs"][0]["code"] == "TXO21900P"

    def test_from_dict_without_combo_fields_remains_backward_compatible(self):
        restored = Order.from_dict({
            "order_id": "ORD-LEGACY",
            "intent_id": "intent-legacy",
            "symbol": "TMF",
            "side": "buy",
            "order_type": "market",
            "quantity": 1,
            "filled_quantity": 0,
            "price": None,
            "stop_price": None,
            "avg_fill_price": 0.0,
            "status": "pending_submit",
            "strategy": "",
            "account": "",
            "comment": "",
            "commission": 0.0,
            "tax": 0.0,
            "total_fee": 0.0,
            "slippage": 0.0,
            "fill_time_ms": None,
            "exchange_order_id": None,
            "broker_order_id": None,
            "seqno": None,
            "ordno": None,
            "reject_reason": None,
            "cancel_reason": None,
            "parent_order_id": None,
            "fills": [],
            "raw_events": [],
            "created_at": datetime.now().isoformat(),
            "submitted_at": None,
            "filled_at": None,
            "cancelled_at": None,
            "rejected_at": None,
            "expired_at": None,
            "updated_at": datetime.now().isoformat(),
        })

        assert restored.truth_source in ("", None)
        assert restored.combo_legs == []
        assert restored.combo_strategy == ""


# ── L1-UT-09: Order Events ──

class TestOrderEvents:
    def test_on_fill_emits_event(self, paper_mgr):
        events = []
        paper_mgr.register_callback("on_fill", lambda evt: events.append(evt))

        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.on_fill(order.order_id, 36450, 1, partial=False)

        assert len(events) == 1
        assert events[0].order_id == order.order_id
        assert events[0].status == OrderStatus.FILLED
        assert events[0].fill_price == 36450

    def test_on_cancel_emits_event(self, paper_mgr):
        events = []
        paper_mgr.register_callback("on_cancel", lambda evt: events.append(evt))

        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        paper_mgr.cancel(order.order_id, reason="test")

        assert len(events) == 1
        assert events[0].order_id == order.order_id
        assert events[0].status == OrderStatus.CANCELLED


# ── L1-UT-10: Edge Cases ──

class TestEdgeCases:
    def test_fill_unknown_order_id(self, paper_mgr):
        """on_fill with unknown order_id should not crash"""
        paper_mgr.on_fill("NONEXISTENT", 36450, 1, partial=False)
        # Should silently ignore

    def test_cancel_unknown_order_id(self, paper_mgr):
        with pytest.raises(KeyError):
            paper_mgr.cancel("NONEXISTENT")

    def test_fill_exceeds_remaining(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 2)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        with pytest.raises(ValueError, match="exceeds remaining"):
            paper_mgr.on_fill(order.order_id, 36450, 3, partial=False)

    def test_fill_zero_quantity(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        with pytest.raises(ValueError, match="positive"):
            paper_mgr.on_fill(order.order_id, 36450, 0, partial=False)

    def test_fill_negative_price(self, paper_mgr):
        order = paper_mgr.create_order("TMF", OrderSide.BUY, OrderType.MARKET, 1)
        paper_mgr.submit(order, exchange_ordno="EXCH-001")
        # Negative price should be allowed (data error, not logic error)
        paper_mgr.on_fill(order.order_id, -1, 1, partial=False)
        assert order.avg_fill_price == -1
