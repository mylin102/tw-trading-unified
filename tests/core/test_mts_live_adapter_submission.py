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


def test_live_partial_submission_disables_watchdog_cancel_and_records_reconcile():
    """An acknowledged first leg must be operator-reconciled, never auto-cancelled."""
    from core.mode_transition import (ExecutionContext, ModeTransitionState)
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._execution_context = ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
    )
    monitor._mts_stale_order_cancels = set()
    recorded = []
    monitor._record_mts_entry_reconcile = lambda trade_id: recorded.append(trade_id)
    monitor._persist_execution_context = lambda: None
    monitor._append_mts_event = lambda *args, **kwargs: None
    near = SimpleNamespace(order_id="near-1", exchange_order_id="BRK-NEAR")

    monitor._quarantine_mts_entry_partial_submission(
        trade_id="trade-1", submitted_order=near,
        failed_order=SimpleNamespace(order_id="far-1"))

    assert recorded == ["trade-1"]
    assert monitor._mts_stale_order_cancels == {"near-1"}


def test_pending_entry_reconcile_requarantines_after_certification_attempt():
    """A restart/recertification cannot clear durable entry reconciliation."""
    from core.mode_transition import (ExecutionContext, ModeTransitionState)
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._execution_context = ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
    )
    persisted = []
    monitor._persist_execution_context = lambda: persisted.append(
        monitor._execution_context)
    monitor._pending_safety_stop_reconcile = lambda: False
    monitor._pending_mts_entry_reconcile = lambda: True

    monitor._apply_reconcile_pending_gate()

    assert monitor._execution_context.effective_mode == (
        ModeTransitionState.LIVE_QUARANTINED.value)
    assert monitor._execution_context.live_order_allowed is False
    assert "MTS_ENTRY_RECONCILE_PENDING" in monitor._execution_context.audit_reasons
    assert persisted == [monitor._execution_context]


def test_pending_entry_reconcile_fails_post_startup_gate_before_recertification():
    """No startup/reconnect certificate transition may bypass reconciliation."""
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._pending_safety_stop_reconcile = lambda: False
    monitor._pending_mts_entry_reconcile = lambda: True

    gate, evidence = monitor._run_post_startup_gate()

    assert gate.ok is False
    assert evidence is None
    assert gate.results[0].guard == "reconciliation"
    assert gate.results[0].reasons == ("MTS_ENTRY_RECONCILE_PENDING",)


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


def test_persist_syncs_live_context_to_wrapped_adapter_without_prior_attr(monkeypatch):
    """A ``__new__``-wrapped live adapter must still receive LIVE_READY."""
    from core import execution_context_state
    from core.mode_transition import ExecutionContext, ModeTransitionState
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.client = SimpleNamespace()
    monitor._execution_context = ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
    )
    monkeypatch.setattr(execution_context_state, "persist_execution_context", lambda payload: None)

    monitor._persist_execution_context()

    assert monitor.client._execution_context is monitor._execution_context


def test_mts_near_rejection_terminally_rejects_unsubmitted_far(monkeypatch):
    """A failed first entry leg cannot leave an unsubmitted far leg for watchdog."""
    from datetime import datetime
    from strategies.futures import monitor as monitor_module
    from strategies.futures.monitor import FuturesMonitor

    manager = OrderManager(mode="paper")
    submitted, events = [], []

    def reject_near(order):
        submitted.append(order.symbol)
        manager.reject(order.order_id, reason="ADAPTER_SUBMIT_FAILED", source="test")
        return False

    manager.submit = reject_near
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor._execution_context = None
    monitor.order_mgr = manager
    monitor.paper_fill_sim = None
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._pending_lifecycle_orders = {}
    monitor._mts_pending_fills = {}
    monitor._mts_block_entry_if_open_position = lambda strategy, action: False
    monitor._append_mts_event = lambda kind, **payload: events.append((kind, payload))
    monkeypatch.setattr(monitor_module, "is_taifex_futures_market_open", lambda: True)

    monitor._submit_mts_order_signal(
        SimpleNamespace(action="SELL_NEAR_BUY_FAR", reason="test"),
        SimpleNamespace(),
        {"near_close": 100.0, "far_close": 110.0},
        datetime(2026, 8, 10, 20, 6, 40),
    )

    assert submitted == ["TMFH6"]
    assert manager.active_orders == {}
    assert [order.status for order in manager.completed] == [OrderStatus.REJECTED, OrderStatus.REJECTED]
    assert manager.completed[1].reject_reason == "PRECEDING_LEG_REJECTED"
    assert [kind for kind, _ in events].count("ORDER_SUBMITTED") == 0


def test_live_typed_adapter_failure_preserves_stable_reason():
    """The dashboard/event path must retain the adapter code, not erase it."""
    class CodedFailure:
        def place_order_object(self, order):
            error = RuntimeError("broker boundary refused")
            error.code = "ADAPTER_ORDER_PLACE_FAILED"
            raise error

    manager = OrderManager(mode="live", broker_adapter=CodedFailure())
    order = _new_order(manager)

    assert manager.submit(order) is False
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == "ADAPTER_ORDER_PLACE_FAILED"


def test_live_unrecognized_adapter_code_is_redacted():
    """Exception-controlled adapter text never reaches operator surfaces."""
    class CodedFailure:
        def place_order_object(self, order):
            error = RuntimeError("broker context must not be exposed")
            error.code = "ADAPTER_SECRET: broker context"
            raise error

    manager = OrderManager(mode="live", broker_adapter=CodedFailure())
    order = _new_order(manager)

    assert manager.submit(order) is False
    assert order.status is OrderStatus.REJECTED
    assert order.reject_reason == "ADAPTER_SUBMIT_FAILED"
