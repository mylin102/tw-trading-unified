"""Regression coverage for the live MTS order-manager adapter boundary."""

from types import SimpleNamespace

from core.order_management.order import OrderSide, OrderStatus, OrderType
from core.order_management.order_manager import OrderManager
from strategies.futures.squeeze_futures.data.shioaji_client import ShioajiClient


def _new_order(manager):
    return manager.create_order(
        symbol="TMFH6", side=OrderSide.SELL, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_ENTRY",
    )


def test_shioaji_object_bridge_passes_contract_action_and_quantity():
    """The domain Order never reaches the positional broker API directly."""
    client = ShioajiClient.__new__(ShioajiClient)
    contract = SimpleNamespace(code="TMFH6")
    received = {}
    client.get_contract = lambda symbol: contract

    def place_order(actual_contract, action, quantity, price=0):
        received.update(contract=actual_contract, action=action,
                        quantity=quantity, price=price)
        return "receipt"

    client.place_order = place_order
    manager = OrderManager(mode="paper")
    result = client.place_order_object(_new_order(manager))

    assert result == "receipt"
    assert received == {
        "contract": contract, "action": "sell", "quantity": 1, "price": 0,
    }


def test_shioaji_exact_contract_resolver_never_guesses_roll_code(monkeypatch):
    """The live bridge must resolve an exact code, never a product alias."""
    from core.broker import shioaji_compat

    wanted = SimpleNamespace(code="TMFH6")
    other = SimpleNamespace(code="TMFI6")
    monkeypatch.setattr(
        shioaji_compat,
        "get_contracts_list",
        lambda api, kind, product: [other, wanted],
    )
    client = ShioajiClient.__new__(ShioajiClient)
    client.api = object()
    client.is_logged_in = True

    assert client.get_contract("TMFH6") is wanted
    assert client.get_contract("TMFZ9") is None


def test_live_adapter_exception_rejects_without_active_pending_order():
    """A local adapter error is terminal; it cannot become a watchdog timeout."""
    class FailingAdapter:
        def place_order_object(self, order):
            raise TypeError("missing positional arguments")

    manager = OrderManager(mode="live", broker_adapter=FailingAdapter())
    order = _new_order(manager)

    assert manager.submit(order) is False
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == "ADAPTER_SUBMIT_FAILED"
    assert order.order_id not in manager.active_orders
    assert manager.completed == [order]


def test_live_missing_broker_receipt_rejects_without_active_pending_order():
    """A response without broker identity is not a submitted exchange order."""
    class ReceiptlessAdapter:
        def place_order_object(self, order):
            return SimpleNamespace(status=SimpleNamespace())

    manager = OrderManager(mode="live", broker_adapter=ReceiptlessAdapter())
    order = _new_order(manager)

    assert manager.submit(order) is False
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == "BROKER_RECEIPT_MISSING"
    assert order.order_id not in manager.active_orders
    assert manager.completed == [order]


def test_live_seqno_receipt_never_synthesizes_local_exchange_id():
    """A broker seqno is retained instead of substituting our order id."""
    class SeqnoAdapter:
        def place_order_object(self, order):
            return SimpleNamespace(status=SimpleNamespace(seqno="SEQ-9001"))

    manager = OrderManager(mode="live", broker_adapter=SeqnoAdapter())
    order = _new_order(manager)

    assert manager.submit(order) is True
    assert order.seqno == "SEQ-9001"
    assert order.exchange_order_id == "SEQ-9001"
    assert order.exchange_order_id != order.order_id


def test_live_second_leg_rejection_quarantines_without_compensating_order():
    """A submitted near leg plus failed far leg requires reconciliation."""
    from core.mode_transition import (ExecutionContext, ModeTransitionState)
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._execution_context = ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
        audit_reasons=("CERTIFIED",),
    )
    persisted, events = [], []
    monitor._persist_execution_context = lambda: persisted.append(
        monitor._execution_context)
    monitor._append_mts_event = lambda kind, **payload: events.append(
        (kind, payload))
    near = SimpleNamespace(order_id="near-1", exchange_order_id="BRK-NEAR")
    far = SimpleNamespace(order_id="far-1", reject_reason="ADAPTER_SUBMIT_FAILED")

    monitor._quarantine_mts_entry_partial_submission(
        trade_id="trade-1", submitted_order=near, failed_order=far)

    assert monitor._execution_context.effective_mode == (
        ModeTransitionState.LIVE_QUARANTINED.value)
    assert monitor._execution_context.live_order_allowed is False
    assert persisted == [monitor._execution_context]
    assert events == [("MTS_ENTRY_PARTIAL_SUBMISSION", {
        "trade_id": "trade-1",
        "submitted_order_id": "near-1",
        "submitted_broker_order_id": "BRK-NEAR",
        "failed_order_id": "far-1",
        "reason": "ADAPTER_SUBMIT_FAILED",
    })]


def test_paper_second_leg_rejection_records_evidence_without_live_quarantine():
    """Paper retains its mode even if a test double rejects a second leg."""
    from core.mode_transition import (ExecutionContext, ModeTransitionState)
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._execution_context = ExecutionContext(
        requested_mode="paper",
        effective_mode=ModeTransitionState.PAPER_ACTIVE.value,
    )
    persisted, events = [], []
    monitor._persist_execution_context = lambda: persisted.append(True)
    monitor._append_mts_event = lambda kind, **payload: events.append(kind)

    monitor._quarantine_mts_entry_partial_submission(
        trade_id="paper-trade",
        submitted_order=SimpleNamespace(order_id="near-paper"),
        failed_order=SimpleNamespace(order_id="far-paper"))

    assert monitor._execution_context.effective_mode == (
        ModeTransitionState.PAPER_ACTIVE.value)
    assert persisted == []
    assert events == ["MTS_ENTRY_PARTIAL_SUBMISSION"]
