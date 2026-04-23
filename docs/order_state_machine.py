"""
order_state_machine.py

Order state machine for futures auto-trading.
Designed to sit between strategy_router and broker execution.

Goals:
- explicit states and transitions
- idempotent event handling
- duplicate-submit protection
- clean reconciliation hooks
- safe timeout / cancel / reject behavior

This module intentionally does NOT talk to a broker directly.
It only models order lifecycle state and transition rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import uuid


UTC = timezone.utc


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderIntent(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    CANCEL = "CANCEL"


class OrderState(str, Enum):
    CREATED = "CREATED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    ERROR = "ERROR"


class OrderEvent(str, Enum):
    CREATE = "CREATE"
    SUBMIT_START = "SUBMIT_START"
    SUBMIT_OK = "SUBMIT_OK"
    ACK = "ACK"
    PARTIAL_FILL = "PARTIAL_FILL"
    FILL = "FILL"
    CANCEL_REQUEST = "CANCEL_REQUEST"
    CANCEL_ACK = "CANCEL_ACK"
    REJECT = "REJECT"
    EXPIRE = "EXPIRE"
    FAIL = "FAIL"
    TIMEOUT = "TIMEOUT"
    RECONCILE_MISSING = "RECONCILE_MISSING"
    RECONCILE_BROKER_OPEN = "RECONCILE_BROKER_OPEN"
    RECONCILE_BROKER_FILLED = "RECONCILE_BROKER_FILLED"
    RECONCILE_BROKER_CANCELED = "RECONCILE_BROKER_CANCELED"


FINAL_STATES = {
    OrderState.FILLED,
    OrderState.CANCELED,
    OrderState.REJECTED,
    OrderState.EXPIRED,
    OrderState.ERROR,
}

WORKING_STATES = {
    OrderState.SUBMITTING,
    OrderState.SUBMITTED,
    OrderState.ACKNOWLEDGED,
    OrderState.PARTIALLY_FILLED,
    OrderState.CANCEL_PENDING,
}


@dataclass(frozen=True)
class StateTransition:
    event: OrderEvent
    from_state: OrderState
    to_state: OrderState
    at: datetime
    note: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderRecord:
    client_order_id: str
    signal_id: str
    symbol: str
    side: OrderSide
    intent: OrderIntent
    quantity: int
    price: Optional[float] = None
    broker_order_id: Optional[str] = None
    state: OrderState = OrderState.CREATED
    filled_qty: int = 0
    avg_fill_price: float = 0.0
    reject_reason: Optional[str] = None
    cancel_reason: Optional[str] = None
    error_reason: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    submitted_at: Optional[datetime] = None
    acknowledged_at: Optional[datetime] = None
    final_at: Optional[datetime] = None
    last_broker_sync_at: Optional[datetime] = None
    transitions: List[StateTransition] = field(default_factory=list)

    @property
    def remaining_qty(self) -> int:
        return max(self.quantity - self.filled_qty, 0)

    @property
    def is_final(self) -> bool:
        return self.state in FINAL_STATES

    @property
    def is_working(self) -> bool:
        return self.state in WORKING_STATES


@dataclass(frozen=True)
class StateMachineConfig:
    submit_timeout_seconds: int = 8
    ack_timeout_seconds: int = 15
    cancel_timeout_seconds: int = 10
    auto_cancel_on_timeout: bool = True
    reconcile_missing_as_error_after_seconds: int = 20


class InvalidTransitionError(Exception):
    pass


class OrderStateMachine:
    """Deterministic order state machine with idempotent event application."""

    def __init__(self, config: Optional[StateMachineConfig] = None):
        self.config = config or StateMachineConfig()
        self._records: Dict[str, OrderRecord] = {}
        self._signal_guard: Dict[str, str] = {}

    def create_order(
        self,
        signal_id: str,
        symbol: str,
        side: str,
        quantity: int,
        price: Optional[float] = None,
        intent: str = "ENTRY",
        client_order_id: Optional[str] = None,
    ) -> OrderRecord:
        """
        Create a new order record.

        Duplicate guard rule:
        one live order per signal_id.
        """
        existing_id = self._signal_guard.get(signal_id)
        if existing_id:
            existing = self._records[existing_id]
            if not existing.is_final:
                raise InvalidTransitionError(
                    f"live order already exists for signal_id={signal_id}: {existing.client_order_id}"
                )

        oid = client_order_id or self._gen_client_order_id(symbol)
        record = OrderRecord(
            client_order_id=oid,
            signal_id=signal_id,
            symbol=symbol,
            side=OrderSide(side),
            intent=OrderIntent(intent),
            quantity=int(quantity),
            price=price,
        )
        self._records[oid] = record
        self._signal_guard[signal_id] = oid
        self._append_transition(record, OrderEvent.CREATE, OrderState.CREATED, note="order created")
        return record

    def get(self, client_order_id: str) -> OrderRecord:
        return self._records[client_order_id]

    def all_records(self) -> List[OrderRecord]:
        return list(self._records.values())

    def can_submit(self, client_order_id: str) -> bool:
        record = self.get(client_order_id)
        return record.state == OrderState.CREATED

    def begin_submit(self, client_order_id: str) -> OrderRecord:
        record = self.get(client_order_id)
        self._transition(record, OrderEvent.SUBMIT_START, {OrderState.CREATED}, OrderState.SUBMITTING)
        return record

    def mark_submitted(
        self,
        client_order_id: str,
        broker_order_id: Optional[str] = None,
    ) -> OrderRecord:
        record = self.get(client_order_id)
        # idempotent if already submitted/acked later
        if record.state in {OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}:
            if broker_order_id and not record.broker_order_id:
                record.broker_order_id = broker_order_id
            return record

        self._transition(record, OrderEvent.SUBMIT_OK, {OrderState.SUBMITTING}, OrderState.SUBMITTED)
        record.submitted_at = datetime.now(UTC)
        if broker_order_id:
            record.broker_order_id = broker_order_id
        return record

    def acknowledge(
        self,
        client_order_id: str,
        broker_order_id: Optional[str] = None,
    ) -> OrderRecord:
        record = self.get(client_order_id)
        if record.state in {OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.FILLED}:
            if broker_order_id and not record.broker_order_id:
                record.broker_order_id = broker_order_id
            return record

        self._transition(
            record,
            OrderEvent.ACK,
            {OrderState.SUBMITTED, OrderState.SUBMITTING},
            OrderState.ACKNOWLEDGED,
        )
        record.acknowledged_at = datetime.now(UTC)
        if broker_order_id:
            record.broker_order_id = broker_order_id
        return record

    def apply_partial_fill(
        self,
        client_order_id: str,
        fill_qty: int,
        fill_price: float,
    ) -> OrderRecord:
        record = self.get(client_order_id)
        if record.is_final:
            return record

        if record.state not in {OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.CANCEL_PENDING}:
            raise InvalidTransitionError(f"partial fill not allowed from {record.state}")

        new_filled = record.filled_qty + int(fill_qty)
        if new_filled <= 0:
            raise InvalidTransitionError("fill_qty must make filled_qty positive")
        if new_filled > record.quantity:
            raise InvalidTransitionError("filled quantity exceeds order quantity")

        record.avg_fill_price = self._weighted_avg(
            old_qty=record.filled_qty,
            old_avg=record.avg_fill_price,
            new_qty=fill_qty,
            new_price=fill_price,
        )
        record.filled_qty = new_filled

        target_state = OrderState.FILLED if record.filled_qty == record.quantity else OrderState.PARTIALLY_FILLED
        event = OrderEvent.FILL if target_state == OrderState.FILLED else OrderEvent.PARTIAL_FILL
        self._force_state(record, event, target_state, payload={"fill_qty": fill_qty, "fill_price": fill_price})
        if target_state == OrderState.FILLED:
            record.final_at = datetime.now(UTC)
        return record

    def request_cancel(self, client_order_id: str, reason: str = "") -> OrderRecord:
        record = self.get(client_order_id)
        if record.is_final:
            return record
        if record.state == OrderState.CANCEL_PENDING:
            return record
        if record.state not in {OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.SUBMITTING}:
            raise InvalidTransitionError(f"cancel not allowed from {record.state}")

        record.cancel_reason = reason or record.cancel_reason
        self._force_state(record, OrderEvent.CANCEL_REQUEST, OrderState.CANCEL_PENDING, note=reason)
        return record

    def mark_canceled(self, client_order_id: str, reason: str = "") -> OrderRecord:
        record = self.get(client_order_id)
        if record.state == OrderState.CANCELED:
            return record
        allowed = {OrderState.CANCEL_PENDING, OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.SUBMITTING}
        self._transition(record, OrderEvent.CANCEL_ACK, allowed, OrderState.CANCELED, note=reason)
        record.cancel_reason = reason or record.cancel_reason
        record.final_at = datetime.now(UTC)
        return record

    def reject(self, client_order_id: str, reason: str) -> OrderRecord:
        record = self.get(client_order_id)
        if record.state == OrderState.REJECTED:
            return record
        if record.is_final:
            return record
        allowed = {OrderState.CREATED, OrderState.SUBMITTING, OrderState.SUBMITTED, OrderState.ACKNOWLEDGED}
        self._transition(record, OrderEvent.REJECT, allowed, OrderState.REJECTED, note=reason)
        record.reject_reason = reason
        record.final_at = datetime.now(UTC)
        return record

    def expire(self, client_order_id: str, reason: str = "expired by broker") -> OrderRecord:
        record = self.get(client_order_id)
        if record.state == OrderState.EXPIRED:
            return record
        if record.is_final:
            return record
        allowed = {OrderState.SUBMITTED, OrderState.ACKNOWLEDGED, OrderState.PARTIALLY_FILLED, OrderState.CANCEL_PENDING}
        self._transition(record, OrderEvent.EXPIRE, allowed, OrderState.EXPIRED, note=reason)
        record.final_at = datetime.now(UTC)
        return record

    def fail(self, client_order_id: str, reason: str) -> OrderRecord:
        record = self.get(client_order_id)
        if record.state == OrderState.ERROR:
            return record
        if record.is_final:
            return record
        self._force_state(record, OrderEvent.FAIL, OrderState.ERROR, note=reason)
        record.error_reason = reason
        record.final_at = datetime.now(UTC)
        return record

    def on_timeout(self, client_order_id: str, now: Optional[datetime] = None) -> OrderRecord:
        """Apply timeout policy based on current state age."""
        record = self.get(client_order_id)
        if record.is_final:
            return record

        now = now or datetime.now(UTC)
        age = now - record.updated_at

        if record.state == OrderState.SUBMITTING and age >= timedelta(seconds=self.config.submit_timeout_seconds):
            if self.config.auto_cancel_on_timeout:
                return self.request_cancel(client_order_id, reason="submit timeout")
            return self.fail(client_order_id, reason="submit timeout")

        if record.state in {OrderState.SUBMITTED, OrderState.ACKNOWLEDGED} and age >= timedelta(seconds=self.config.ack_timeout_seconds):
            if self.config.auto_cancel_on_timeout:
                return self.request_cancel(client_order_id, reason="ack/fill timeout")
            return self.fail(client_order_id, reason="ack/fill timeout")

        if record.state == OrderState.CANCEL_PENDING and age >= timedelta(seconds=self.config.cancel_timeout_seconds):
            return self.fail(client_order_id, reason="cancel timeout")

        return record

    def reconcile(
        self,
        client_order_id: str,
        broker_status: str,
        broker_order_id: Optional[str] = None,
        filled_qty: Optional[int] = None,
        avg_fill_price: Optional[float] = None,
        broker_seen_at: Optional[datetime] = None,
    ) -> OrderRecord:
        """
        Reconcile local state with broker truth.

        broker_status examples:
        - OPEN
        - FILLED
        - CANCELED
        - MISSING
        """
        record = self.get(client_order_id)
        if broker_order_id and not record.broker_order_id:
            record.broker_order_id = broker_order_id
        record.last_broker_sync_at = broker_seen_at or datetime.now(UTC)

        status = broker_status.upper().strip()
        if status == "OPEN":
            self._force_state(record, OrderEvent.RECONCILE_BROKER_OPEN, OrderState.ACKNOWLEDGED, note="broker says order is open")
            return record

        if status == "FILLED":
            if filled_qty is None:
                filled_qty = record.quantity
            if avg_fill_price is not None:
                record.avg_fill_price = avg_fill_price
            record.filled_qty = min(int(filled_qty), record.quantity)
            self._force_state(record, OrderEvent.RECONCILE_BROKER_FILLED, OrderState.FILLED, note="broker says order is filled")
            record.final_at = datetime.now(UTC)
            return record

        if status == "CANCELED":
            self._force_state(record, OrderEvent.RECONCILE_BROKER_CANCELED, OrderState.CANCELED, note="broker says order is canceled")
            record.final_at = datetime.now(UTC)
            return record

        if status == "MISSING":
            elapsed = datetime.now(UTC) - (record.last_broker_sync_at or record.updated_at)
            if elapsed >= timedelta(seconds=self.config.reconcile_missing_as_error_after_seconds):
                self._force_state(record, OrderEvent.RECONCILE_MISSING, OrderState.ERROR, note="broker cannot find order after grace period")
                record.error_reason = "broker cannot find order"
                record.final_at = datetime.now(UTC)
            return record

        raise ValueError(f"unknown broker_status={broker_status}")

    def summary(self, client_order_id: str) -> Dict[str, Any]:
        record = self.get(client_order_id)
        return {
            "client_order_id": record.client_order_id,
            "broker_order_id": record.broker_order_id,
            "signal_id": record.signal_id,
            "symbol": record.symbol,
            "side": record.side.value,
            "intent": record.intent.value,
            "state": record.state.value,
            "quantity": record.quantity,
            "filled_qty": record.filled_qty,
            "remaining_qty": record.remaining_qty,
            "avg_fill_price": record.avg_fill_price,
            "reject_reason": record.reject_reason,
            "cancel_reason": record.cancel_reason,
            "error_reason": record.error_reason,
            "updated_at": record.updated_at.isoformat(),
            "final_at": record.final_at.isoformat() if record.final_at else None,
        }

    def _transition(
        self,
        record: OrderRecord,
        event: OrderEvent,
        allowed_from: set[OrderState],
        to_state: OrderState,
        note: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if record.state not in allowed_from:
            raise InvalidTransitionError(
                f"invalid transition event={event.value} from={record.state.value} to={to_state.value}"
            )
        self._append_transition(record, event, to_state, note=note, payload=payload or {})

    def _force_state(
        self,
        record: OrderRecord,
        event: OrderEvent,
        to_state: OrderState,
        note: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if record.state == to_state:
            return
        self._append_transition(record, event, to_state, note=note, payload=payload or {})

    def _append_transition(
        self,
        record: OrderRecord,
        event: OrderEvent,
        to_state: OrderState,
        note: str = "",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        now = datetime.now(UTC)
        transition = StateTransition(
            event=event,
            from_state=record.state,
            to_state=to_state,
            at=now,
            note=note,
            payload=payload or {},
        )
        record.transitions.append(transition)
        record.state = to_state
        record.updated_at = now

    @staticmethod
    def _weighted_avg(old_qty: int, old_avg: float, new_qty: int, new_price: float) -> float:
        total_qty = old_qty + new_qty
        if total_qty <= 0:
            return 0.0
        return ((old_qty * old_avg) + (new_qty * new_price)) / total_qty

    @staticmethod
    def _gen_client_order_id(symbol: str) -> str:
        suffix = uuid.uuid4().hex[:10]
        return f"{symbol}-{suffix}"


if __name__ == "__main__":
    sm = OrderStateMachine()
    order = sm.create_order(
        signal_id="txf-2026-04-22T01:45:00-long",
        symbol="TXF",
        side="BUY",
        quantity=1,
        price=37810,
        intent="ENTRY",
    )
    sm.begin_submit(order.client_order_id)
    sm.mark_submitted(order.client_order_id, broker_order_id="BRK123")
    sm.acknowledge(order.client_order_id, broker_order_id="BRK123")
    sm.apply_partial_fill(order.client_order_id, fill_qty=1, fill_price=37812)
    print(sm.summary(order.client_order_id))
