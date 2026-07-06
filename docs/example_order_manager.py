from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from threading import RLock
from typing import Any, Callable, Dict, List, Optional
import logging
import time
import uuid

logger = logging.getLogger(__name__)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderState(str, Enum):
    INTENT_CREATED = "INTENT_CREATED"
    SUBMITTING = "SUBMITTING"
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


TERMINAL_STATES = {
    OrderState.FILLED,
    OrderState.CANCELLED,
    OrderState.FAILED,
}

ACTIVE_STATES = {
    OrderState.SUBMITTING,
    OrderState.PENDING_SUBMIT,
    OrderState.SUBMITTED,
    OrderState.PARTIALLY_FILLED,
    OrderState.CANCEL_REQUESTED,
    OrderState.UNKNOWN,
}


@dataclass
class Intent:
    intent_id: str
    strategy_id: str
    signal_id: str
    contract_code: str
    side: Side
    quantity: int
    price: Optional[float]
    created_at: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FillRecord:
    fill_id: str
    order_id: str
    broker_trade_id: Optional[str]
    exchange_seq: Optional[str]
    price: float
    quantity: int
    timestamp: datetime
    raw_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderRecord:
    order_id: str
    intent_id: str
    state: OrderState
    quantity: int
    filled_quantity: int = 0
    remaining_quantity: int = 0
    price: Optional[float] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    last_event_at: Optional[datetime] = None

    broker_order_id: Optional[str] = None
    seqno: Optional[str] = None
    ordno: Optional[str] = None

    shioaji_status: Optional[str] = None
    shioaji_status_code: Optional[str] = None

    last_error: Optional[str] = None
    raw_trade: Any = None
    raw_events: List[Dict[str, Any]] = field(default_factory=list)
    fills: List[FillRecord] = field(default_factory=list)

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES


class OrderLifecycleError(Exception):
    pass


class OrderManager:
    """
    Shioaji order lifecycle manager.

    Responsibilities:
    - track intent -> order -> fill
    - normalize callback / polling states
    - prevent duplicate submissions
    - reconcile local state with broker snapshot
    """

    def __init__(
        self,
        api: Any,
        account_getter: Callable[[], Any],
        reconcile_interval_sec: float = 5.0,
        pending_submit_timeout_sec: float = 8.0,
    ) -> None:
        self.api = api
        self.account_getter = account_getter
        self.reconcile_interval_sec = reconcile_interval_sec
        self.pending_submit_timeout_sec = pending_submit_timeout_sec

        self._lock = RLock()
        self._intents: Dict[str, Intent] = {}
        self._orders: Dict[str, OrderRecord] = {}
        self._intent_to_order: Dict[str, str] = {}
        self._broker_order_index: Dict[str, str] = {}
        self._seqno_index: Dict[str, str] = {}
        self._ordno_index: Dict[str, str] = {}
        self._alerts: List[Dict[str, Any]] = []
        self._last_reconcile_at: Optional[datetime] = None

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------
    def create_intent(
        self,
        strategy_id: str,
        signal_id: str,
        contract_code: str,
        side: Side,
        quantity: int,
        price: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Intent:
        intent = Intent(
            intent_id=self._new_id("intent"),
            strategy_id=strategy_id,
            signal_id=signal_id,
            contract_code=contract_code,
            side=side,
            quantity=quantity,
            price=price,
            metadata=metadata or {},
        )
        with self._lock:
            self._intents[intent.intent_id] = intent
        logger.info("intent_created intent_id=%s strategy=%s signal=%s contract=%s side=%s qty=%s",
                    intent.intent_id, strategy_id, signal_id, contract_code, side.value, quantity)
        return intent

    def submit_order(self, intent: Intent, contract: Any, order: Any) -> OrderRecord:
        with self._lock:
            self._guard_duplicate_submit(intent)
            order_id = self._new_id("order")
            record = OrderRecord(
                order_id=order_id,
                intent_id=intent.intent_id,
                state=OrderState.SUBMITTING,
                quantity=intent.quantity,
                remaining_quantity=intent.quantity,
                price=intent.price,
            )
            self._orders[order_id] = record
            self._intent_to_order[intent.intent_id] = order_id

        try:
            trade = self.api.place_order(contract, order)
        except Exception as exc:
            with self._lock:
                self._transition(record, OrderState.FAILED, error=str(exc))
            logger.exception("order_submit_failed order_id=%s intent_id=%s", record.order_id, intent.intent_id)
            raise

        with self._lock:
            record.raw_trade = trade
            self._merge_trade_snapshot(record, trade)
            if self._is_nonblocking_inactive(trade):
                self._transition(record, OrderState.PENDING_SUBMIT)
            else:
                next_state = self._map_trade_to_state(trade)
                self._transition(record, next_state)

        logger.info("order_submitted order_id=%s intent_id=%s state=%s broker_order_id=%s seqno=%s ordno=%s",
                    record.order_id, record.intent_id, record.state.value,
                    record.broker_order_id, record.seqno, record.ordno)
        return record

    def cancel_order(self, order_id: str) -> None:
        with self._lock:
            record = self._require_order(order_id)
            if record.is_terminal():
                logger.warning("cancel_skipped_terminal order_id=%s state=%s", order_id, record.state.value)
                return

        self.refresh_order(order_id)

        with self._lock:
            record = self._require_order(order_id)
            if record.state == OrderState.FILLED:
                logger.warning("cancel_blocked_filled order_id=%s", order_id)
                return
            self._transition(record, OrderState.CANCEL_REQUESTED)
            trade = record.raw_trade

        self.api.cancel_order(trade)
        logger.info("cancel_requested order_id=%s broker_order_id=%s", order_id, record.broker_order_id)

    def refresh_order(self, order_id: str) -> None:
        with self._lock:
            record = self._require_order(order_id)
            trade = record.raw_trade
            if trade is None:
                return
        account = self.account_getter()
        try:
            self.api.update_status(account=account, trade=trade)
        except TypeError:
            self.api.update_status(account=account)
        with self._lock:
            self._merge_trade_snapshot(record, trade)
            mapped = self._map_trade_to_state(trade)
            self._transition(record, mapped, allow_unknown=True)
        logger.info("order_refreshed order_id=%s state=%s", order_id, record.state.value)

    def reconcile_all(self) -> None:
        with self._lock:
            active_order_ids = [oid for oid, rec in self._orders.items() if rec.is_active()]

        for order_id in active_order_ids:
            try:
                self.refresh_order(order_id)
                self._run_sanity_checks(order_id)
            except Exception as exc:
                logger.exception("reconcile_failed order_id=%s err=%s", order_id, exc)
                self._emit_alert("RECONCILE_FAILED", {"order_id": order_id, "error": str(exc)})

        with self._lock:
            self._last_reconcile_at = datetime.utcnow()

    def run_reconcile_forever(self, stop_flag: Callable[[], bool]) -> None:
        while not stop_flag():
            self.reconcile_all()
            time.sleep(self.reconcile_interval_sec)

    def handle_order_event(self, stat: Any, msg: Dict[str, Any]) -> None:
        """
        Attach this to Shioaji order callback wrapper.
        `msg` should be the normalized payload dict from callback.
        """
        order_id = self._resolve_local_order_id(msg)
        if not order_id:
            self._emit_alert("ORDER_EVENT_UNMATCHED", {"payload": msg})
            logger.warning("order_event_unmatched payload=%s", msg)
            return

        with self._lock:
            record = self._orders[order_id]
            record.raw_events.append({"type": "order", "payload": msg, "ts": datetime.utcnow().isoformat()})
            self._merge_callback_payload(record, msg)
            next_state = self._map_callback_status_to_state(msg)
            self._transition(record, next_state, allow_unknown=True)

        logger.info("order_event_applied order_id=%s state=%s", order_id, record.state.value)

    def handle_deal_event(self, stat: Any, msg: Dict[str, Any]) -> None:
        order_id = self._resolve_local_order_id(msg)
        if not order_id:
            self._emit_alert("DEAL_EVENT_UNMATCHED", {"payload": msg})
            logger.warning("deal_event_unmatched payload=%s", msg)
            return

        with self._lock:
            record = self._orders[order_id]
            fill_qty = int(msg.get("quantity") or 0)
            fill_price = float(msg.get("price") or 0)
            fill = FillRecord(
                fill_id=str(msg.get("trade_id") or self._new_id("fill")),
                order_id=record.order_id,
                broker_trade_id=self._safe_str(msg.get("trade_id")),
                exchange_seq=self._safe_str(msg.get("exchange_seq")),
                price=fill_price,
                quantity=fill_qty,
                timestamp=self._parse_ts(msg.get("ts")) or datetime.utcnow(),
                raw_payload=msg,
            )
            record.fills.append(fill)
            record.raw_events.append({"type": "deal", "payload": msg, "ts": datetime.utcnow().isoformat()})
            record.filled_quantity += fill_qty
            record.remaining_quantity = max(record.quantity - record.filled_quantity, 0)
            record.updated_at = datetime.utcnow()
            if record.remaining_quantity == 0:
                self._transition(record, OrderState.FILLED, allow_unknown=True)
            else:
                self._transition(record, OrderState.PARTIALLY_FILLED, allow_unknown=True)

        logger.info("deal_event_applied order_id=%s fill_qty=%s remaining=%s state=%s",
                    order_id, fill_qty, record.remaining_quantity, record.state.value)

    def get_order(self, order_id: str) -> OrderRecord:
        with self._lock:
            return self._orders[order_id]

    def get_alerts(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self._alerts)

    # -----------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------
    def _guard_duplicate_submit(self, intent: Intent) -> None:
        existing_order_id = self._intent_to_order.get(intent.intent_id)
        if existing_order_id:
            existing = self._orders[existing_order_id]
            if existing.is_active():
                raise OrderLifecycleError(
                    f"Active order already exists for intent {intent.intent_id}: {existing.order_id}"
                )

    def _resolve_local_order_id(self, payload: Dict[str, Any]) -> Optional[str]:
        for key in [payload.get("id"), payload.get("order_id")]:
            if key and key in self._broker_order_index:
                return self._broker_order_index[key]
        seqno = payload.get("seqno")
        if seqno and seqno in self._seqno_index:
            return self._seqno_index[seqno]
        ordno = payload.get("ordno")
        if ordno and ordno in self._ordno_index:
            return self._ordno_index[ordno]
        return None

    def _merge_trade_snapshot(self, record: OrderRecord, trade: Any) -> None:
        order_obj = getattr(trade, "order", None)
        status_obj = getattr(trade, "status", None)

        broker_order_id = self._safe_str(getattr(order_obj, "id", None))
        seqno = self._safe_str(getattr(status_obj, "seqno", None) or getattr(order_obj, "seqno", None))
        ordno = self._safe_str(getattr(status_obj, "ordno", None) or getattr(order_obj, "ordno", None))

        if broker_order_id:
            record.broker_order_id = broker_order_id
            self._broker_order_index[broker_order_id] = record.order_id
        if seqno:
            record.seqno = seqno
            self._seqno_index[seqno] = record.order_id
        if ordno:
            record.ordno = ordno
            self._ordno_index[ordno] = record.order_id

        record.shioaji_status = self._safe_str(getattr(status_obj, "status", None))
        record.shioaji_status_code = self._safe_str(getattr(status_obj, "status_code", None))
        record.updated_at = datetime.utcnow()

        deals = getattr(status_obj, "deals", None)
        if deals:
            observed_qty = 0
            for deal in deals:
                dq = int(getattr(deal, "quantity", 0) or 0)
                observed_qty += dq
            if observed_qty >= record.filled_quantity:
                record.filled_quantity = observed_qty
                record.remaining_quantity = max(record.quantity - record.filled_quantity, 0)

    def _merge_callback_payload(self, record: OrderRecord, payload: Dict[str, Any]) -> None:
        for key_name, index in [("id", self._broker_order_index), ("seqno", self._seqno_index), ("ordno", self._ordno_index)]:
            value = self._safe_str(payload.get(key_name))
            if value:
                index[value] = record.order_id
                if key_name == "id":
                    record.broker_order_id = value
                elif key_name == "seqno":
                    record.seqno = value
                elif key_name == "ordno":
                    record.ordno = value

        status = payload.get("status")
        if status:
            record.shioaji_status = self._safe_str(status)
        record.updated_at = datetime.utcnow()

    def _map_trade_to_state(self, trade: Any) -> OrderState:
        status_obj = getattr(trade, "status", None)
        raw_status = self._safe_str(getattr(status_obj, "status", None))
        return self._map_status_string(raw_status)

    def _map_callback_status_to_state(self, payload: Dict[str, Any]) -> OrderState:
        return self._map_status_string(self._safe_str(payload.get("status")))

    def _map_status_string(self, raw_status: Optional[str]) -> OrderState:
        mapping = {
            "PendingSubmit": OrderState.PENDING_SUBMIT,
            "PreSubmitted": OrderState.PENDING_SUBMIT,
            "Inactive": OrderState.PENDING_SUBMIT,
            "Submitted": OrderState.SUBMITTED,
            "PartFilled": OrderState.PARTIALLY_FILLED,
            "Filled": OrderState.FILLED,
            "Cancelled": OrderState.CANCELLED,
            "Failed": OrderState.FAILED,
        }
        if not raw_status:
            return OrderState.UNKNOWN
        return mapping.get(raw_status, OrderState.UNKNOWN)

    def _is_nonblocking_inactive(self, trade: Any) -> bool:
        status_obj = getattr(trade, "status", None)
        raw_status = self._safe_str(getattr(status_obj, "status", None))
        return raw_status == "Inactive"

    def _transition(
        self,
        record: OrderRecord,
        next_state: OrderState,
        error: Optional[str] = None,
        allow_unknown: bool = False,
    ) -> None:
        current = record.state
        if next_state == OrderState.UNKNOWN and not allow_unknown:
            return
        if current == next_state:
            record.updated_at = datetime.utcnow()
            return
        if current in TERMINAL_STATES and next_state not in {current, OrderState.UNKNOWN}:
            self._emit_alert(
                "ILLEGAL_STATE_TRANSITION",
                {"order_id": record.order_id, "current": current.value, "next": next_state.value},
            )
            logger.warning("illegal_transition order_id=%s current=%s next=%s",
                           record.order_id, current.value, next_state.value)
            return

        record.state = next_state
        record.updated_at = datetime.utcnow()
        record.last_event_at = datetime.utcnow()
        if error:
            record.last_error = error

    def _run_sanity_checks(self, order_id: str) -> None:
        with self._lock:
            record = self._orders[order_id]
            now = datetime.utcnow()
            if record.state == OrderState.PENDING_SUBMIT:
                age = now - record.updated_at
                if age > timedelta(seconds=self.pending_submit_timeout_sec):
                    self._emit_alert(
                        "PENDING_SUBMIT_TIMEOUT",
                        {"order_id": order_id, "seconds": age.total_seconds()},
                    )

            if record.filled_quantity > record.quantity:
                self._emit_alert(
                    "FILLED_QTY_EXCEEDS_ORDER_QTY",
                    {
                        "order_id": order_id,
                        "filled_quantity": record.filled_quantity,
                        "quantity": record.quantity,
                    },
                )

            if record.remaining_quantity < 0:
                self._emit_alert(
                    "NEGATIVE_REMAINING_QTY",
                    {
                        "order_id": order_id,
                        "remaining_quantity": record.remaining_quantity,
                    },
                )

    def _emit_alert(self, code: str, payload: Dict[str, Any]) -> None:
        alert = {
            "code": code,
            "payload": payload,
            "ts": datetime.utcnow().isoformat(),
        }
        with self._lock:
            self._alerts.append(alert)
        logger.warning("alert code=%s payload=%s", code, payload)

    def _require_order(self, order_id: str) -> OrderRecord:
        if order_id not in self._orders:
            raise KeyError(f"Unknown order_id: {order_id}")
        return self._orders[order_id]

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _safe_str(value: Any) -> Optional[str]:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        try:
            # adapt if your callback ts is epoch or formatted string
            if isinstance(value, (int, float)):
                return datetime.utcfromtimestamp(value)
            return datetime.fromisoformat(str(value))
        except Exception:
            return None


# ---------------------------------------------------------------
# Example integration hooks
# ---------------------------------------------------------------

def make_order_callback(order_manager: OrderManager):
    def _callback(stat: Any, msg: Dict[str, Any]) -> None:
        event_type = str(msg.get("event") or msg.get("operation") or "").lower()
        if "deal" in event_type:
            order_manager.handle_deal_event(stat, msg)
        else:
            order_manager.handle_order_event(stat, msg)
    return _callback


# Example usage:
# api.set_order_callback(make_order_callback(order_manager))
# intent = order_manager.create_intent(...)
# record = order_manager.submit_order(intent, contract, order)
# order_manager.reconcile_all()
