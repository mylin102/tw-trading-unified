"""
統一委託單管理器 (OrderManager)
Paper / Live 共用同一套狀態機。

🛑 刪除單限制: 只允許 SUBMITTED / PARTIAL_FILLED 狀態取消。
⚡ 重啟恢復: 呼叫 recover_from_api() 重建狀態表。
"""
from __future__ import annotations

import os
import json
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field

from core.order_management.order import Order, OrderStatus, OrderType, OrderSide


@dataclass
class OrderEvent:
    """訂單事件（用於 callback 通知）"""
    order_id: str
    status: OrderStatus
    symbol: str
    side: OrderSide
    intent_id: Optional[str] = None
    deal_id: Optional[str] = None
    broker_order_id: Optional[str] = None
    seqno: Optional[str] = None
    ordno: Optional[str] = None
    fill_price: Optional[float] = None
    fill_qty: int = 0
    reason: str = ""
    raw_status: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)


class OrderManager:
    """
    統一委託單管理器。

    Args:
        mode: "paper" 或 "live"
        broker_adapter: Live 模式下需要，Paper 模式下不需要
    """

    def __init__(self, mode: str = "paper", broker_adapter=None):
        self.mode = mode
        self.active_orders: Dict[str, Order] = {}
        self.completed: List[Order] = []
        # 2026-07-07 Hermes Agent: session_date-based counter reset.
        # Counter resets per trading session, NOT per PM2 restart.
        # Uses get_session_date_str which handles night session crossing midnight.
        try:
            from core.date_utils import get_session_date_str
            self._session_date = get_session_date_str()
        except Exception:
            self._session_date = datetime.now().strftime("%Y%m%d")
        self._next_id = 1
        self.broker_adapter = broker_adapter

        # 2026-07-07 Hermes Agent: reindex from disk immediately after init
        # so the counter survives PM2 restart even before the first caller
        # remembers to call reindex_orders().
        self.reindex_orders()

        # Callback 系統
        self._callbacks: Dict[str, List[Callable]] = {
            "on_fill": [],
            "on_cancel": [],
            "on_reject": [],
            "on_expire": [],
            "on_status_change": [],
        }

        # Simulator reference (for paper mode cleanup)
        self._simulator = None

    def set_simulator(self, sim) -> None:
        """連結 PaperFillSimulator，用於 cancel/reject 時同步移除"""
        self._simulator = sim

    @staticmethod
    def _status_value(status) -> Optional[str]:
        if status is None:
            return None
        if isinstance(status, OrderStatus):
            return status.value
        return str(getattr(status, "value", status))

    @staticmethod
    def _payload_to_dict(payload) -> Optional[Dict[str, Any]]:
        def serialize(value):
            if value is None or isinstance(value, (str, int, float, bool)):
                return value
            if isinstance(value, datetime):
                return value.isoformat()
            if isinstance(value, dict):
                return {key: serialize(item) for key, item in value.items()}
            if isinstance(value, (list, tuple, set)):
                return [serialize(item) for item in value]
            if hasattr(value, "value") and isinstance(getattr(value, "value"), (str, int, float, bool)):
                return value.value
            if hasattr(value, "__dict__"):
                return {
                    key: serialize(item)
                    for key, item in vars(value).items()
                    if not key.startswith("_")
                }
            return str(value)

        if payload is None:
            return None
        if isinstance(payload, dict):
            return serialize(payload)
        if hasattr(payload, "__dict__"):
            return serialize(payload)
        return {"value": serialize(payload)}

    @staticmethod
    def _extract_value(payload, *keys):
        for key in keys:
            if isinstance(payload, dict) and payload.get(key) is not None:
                return payload[key]
            value = getattr(payload, key, None)
            if value is not None:
                return value
        return None

    def _record_audit(
        self,
        order: Order,
        event_type: str,
        *,
        source: str = "",
        reason: str = "",
        from_status=None,
        to_status=None,
        raw_status=None,
        payload=None,
        broker_order_id: Optional[str] = None,
        seqno: Optional[str] = None,
        ordno: Optional[str] = None,
        deal_id: Optional[str] = None,
        fill_price: Optional[float] = None,
        fill_qty: int = 0,
    ) -> Dict[str, Any]:
        entry = {
            "type": event_type,
            "timestamp": datetime.now().isoformat(),
            "source": source or "local",
            "reason": reason,
            "from_status": self._status_value(from_status),
            "to_status": self._status_value(to_status or order.status),
            "raw_status": self._status_value(raw_status),
            "broker_order_id": broker_order_id or order.broker_order_id or order.exchange_order_id,
            "seqno": seqno or order.seqno,
            "ordno": ordno or order.ordno or order.exchange_order_id,
            "deal_id": deal_id,
            "fill_price": fill_price,
            "fill_qty": fill_qty,
        }
        payload_dict = self._payload_to_dict(payload)
        if payload_dict is not None:
            entry["payload"] = payload_dict
        order.raw_events.append(entry)
        return entry

    def _has_fill_identity(
        self,
        order: Order,
        *,
        deal_id: Optional[str] = None,
        broker_trade_id: Optional[str] = None,
        exchange_fill_id: Optional[str] = None,
        exchange_seq: Optional[str] = None,
    ) -> bool:
        for fill in order.fills:
            if deal_id and fill.deal_id == deal_id:
                return True
            if broker_trade_id and fill.broker_trade_id == broker_trade_id:
                return True
            if exchange_fill_id and fill.exchange_fill_id == exchange_fill_id:
                return True
            if exchange_seq and fill.exchange_seq == exchange_seq:
                return True
        return False

    # ── Create ──

    def create_order(
        self,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        quantity: int,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
        strategy: str = "",
        account: str = "",
        comment: str = "",
        truth_source: str = "",
        combo_legs: Optional[List[Dict[str, Any]]] = None,
        combo_strategy: str = "",
    ) -> Order:
        """建立新委託單，狀態→ PENDING_SUBMIT"""
        # 2026-07-07 Hermes Agent: session_date-based order ID.
        # Format: ORD-{session_date}-{counter:06d}
        # Counter resets per trading session, persists across PM2 restarts
        # via reindex_orders() scanning existing orders for current session.
        order_id = f"ORD-{self._session_date}-{self._next_id:06d}"
        self._next_id += 1

        # 2026-07-07 Hermes Agent: collision guard — skip IDs that already
        # exist in active_orders or completed (counter may backtrack after
        # PM2 restart if reindex_orders wasn't called yet).
        _existing_ids = set(self.active_orders.keys()) | {o.order_id for o in self.completed}
        while order_id in _existing_ids:
            order_id = f"ORD-{self._session_date}-{self._next_id:06d}"
            self._next_id += 1

        order = Order(
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            stop_price=stop_price,
            strategy=strategy,
            account=account,
            comment=comment,
            order_id=order_id,
            truth_source=truth_source,
            combo_legs=combo_legs,
            combo_strategy=combo_strategy,
        )
        self.active_orders[order_id] = order
        return order

    def _resolve_order(
        self,
        order_id: Optional[str] = None,
        *,
        broker_order_id: Optional[str] = None,
        seqno: Optional[str] = None,
        ordno: Optional[str] = None,
    ) -> Optional[Order]:
        if order_id:
            order = self.active_orders.get(order_id)
            if order:
                return order
            for completed in self.completed:
                if completed.order_id == order_id:
                    return completed

        for candidate in list(self.active_orders.values()) + self.completed:
            if broker_order_id and candidate.broker_order_id == broker_order_id:
                return candidate
            if seqno and candidate.seqno == seqno:
                return candidate
            if ordno and candidate.ordno == ordno:
                return candidate
            if broker_order_id and candidate.exchange_order_id == broker_order_id:
                return candidate
            if ordno and candidate.exchange_order_id == ordno:
                return candidate
        return None

    @staticmethod
    def _normalize_raw_status(raw_status) -> Optional[OrderStatus]:
        if raw_status is None:
            return None

        value = getattr(raw_status, "value", raw_status)
        if isinstance(value, OrderStatus):
            return value

        normalized = str(value).strip().lower()
        mapping = {
            "pendingsubmit": OrderStatus.PENDING_SUBMIT,
            "pending_submit": OrderStatus.PENDING_SUBMIT,
            "presubmitted": OrderStatus.PRE_SUBMITTED,
            "pre_submitted": OrderStatus.PRE_SUBMITTED,
            "submitted": OrderStatus.SUBMITTED,
            "accepted": OrderStatus.SUBMITTED,
            "partfilled": OrderStatus.PARTIAL_FILLED,
            "partialfilled": OrderStatus.PARTIAL_FILLED,
            "partial_filled": OrderStatus.PARTIAL_FILLED,
            "filled": OrderStatus.FILLED,
            "cancelled": OrderStatus.CANCELLED,
            "canceled": OrderStatus.CANCELLED,
            "failed": OrderStatus.REJECTED,
            "rejected": OrderStatus.REJECTED,
            "expired": OrderStatus.EXPIRED,
        }
        return mapping.get(normalized)

    def attach_submission(
        self,
        order_id: str,
        *,
        broker_trade=None,
        broker_order_id: Optional[str] = None,
        seqno: Optional[str] = None,
        ordno: Optional[str] = None,
        raw_status=None,
        source: str = "",
        reason: str = "",
    ) -> Order:
        order = self._resolve_order(order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found")

        previous_status = order.status
        broker_order_id = (
            broker_order_id
            or getattr(broker_trade, "id", None)
            or getattr(broker_trade, "ordno", None)
            or ordno
        )
        ordno = ordno or getattr(broker_trade, "ordno", None) or broker_order_id
        seqno = seqno or getattr(broker_trade, "seqno", None)
        submit_status = self._normalize_raw_status(raw_status) or OrderStatus.SUBMITTED
        exchange_id = ordno or broker_order_id or order.exchange_order_id or order.order_id

        if order.status == OrderStatus.PENDING_SUBMIT:
            order.submit(
                exchange_id,
                broker_order_id=broker_order_id,
                seqno=seqno,
                ordno=ordno,
            )
        else:
            order.status = submit_status
            order.exchange_order_id = exchange_id
            order.broker_order_id = broker_order_id or exchange_id
            order.seqno = seqno
            order.ordno = ordno or exchange_id
            order.submitted_at = order.submitted_at or datetime.now()
            order.updated_at = datetime.now()

        self._record_audit(
            order,
            "submission",
            source=source or "submission",
            reason=reason,
            from_status=previous_status,
            to_status=order.status,
            raw_status=raw_status,
            payload=broker_trade,
            broker_order_id=broker_order_id,
            seqno=seqno,
            ordno=ordno,
        )
        self._emit("on_status_change", OrderEvent(
            order_id=order.order_id,
            status=order.status,
            symbol=order.symbol,
            side=order.side,
            intent_id=order.intent_id,
            broker_order_id=order.broker_order_id,
            seqno=order.seqno,
            ordno=order.ordno,
            raw_status=str(submit_status.value),
        ))
        return order

    def apply_order_update(
        self,
        order_id: Optional[str],
        *,
        raw_status,
        reason: str = "",
        raw_payload: Optional[Dict[str, Any]] = None,
        broker_order_id: Optional[str] = None,
        seqno: Optional[str] = None,
        ordno: Optional[str] = None,
        source: str = "",
    ) -> Optional[Order]:
        order = self._resolve_order(order_id, broker_order_id=broker_order_id, seqno=seqno, ordno=ordno)
        if order is None:
            return None

        normalized = self._normalize_raw_status(raw_status)
        if normalized is None:
            return order

        previous_status = order.status
        order.broker_order_id = broker_order_id or order.broker_order_id or order.exchange_order_id
        order.seqno = seqno or order.seqno
        order.ordno = ordno or order.ordno or order.exchange_order_id
        self._record_audit(
            order,
            "order_update",
            source=source or "order_update",
            reason=reason,
            from_status=previous_status,
            to_status=normalized,
            raw_status=raw_status,
            payload=raw_payload,
            broker_order_id=order.broker_order_id,
            seqno=order.seqno,
            ordno=order.ordno,
        )

        if normalized in (OrderStatus.PENDING_SUBMIT, OrderStatus.PRE_SUBMITTED, OrderStatus.SUBMITTED):
            order.status = normalized
            if normalized in (OrderStatus.PRE_SUBMITTED, OrderStatus.SUBMITTED):
                order.submitted_at = order.submitted_at or datetime.now()
            order.updated_at = datetime.now()
            self._emit("on_status_change", OrderEvent(
                order_id=order.order_id,
                status=order.status,
                symbol=order.symbol,
                side=order.side,
                intent_id=order.intent_id,
                broker_order_id=order.broker_order_id,
                seqno=order.seqno,
                ordno=order.ordno,
                reason=reason,
                raw_status=str(getattr(raw_status, "value", raw_status)),
            ))
            return order

        if normalized == OrderStatus.PARTIAL_FILLED:
            order.status = OrderStatus.PARTIAL_FILLED
            order.updated_at = datetime.now()
            self._emit("on_status_change", OrderEvent(
                order_id=order.order_id,
                status=order.status,
                symbol=order.symbol,
                side=order.side,
                intent_id=order.intent_id,
                broker_order_id=order.broker_order_id,
                seqno=order.seqno,
                ordno=order.ordno,
                reason=reason,
                raw_status=str(getattr(raw_status, "value", raw_status)),
            ))
            return order

        if normalized == OrderStatus.CANCELLED:
            if order.order_id in self.active_orders:
                self.cancel(order.order_id, reason=reason, source=source or "order_update")
            return order

        if normalized == OrderStatus.REJECTED:
            if order.order_id in self.active_orders:
                self.reject(order.order_id, reason=reason or "rejected", source=source or "order_update")
            return order

        if normalized == OrderStatus.EXPIRED:
            if order.order_id in self.active_orders:
                self.expire(order.order_id, source=source or "order_update", reason=reason)
            return order

        return order

    # ── Submit ──

    def submit(self, order: Order, exchange_ordno: Optional[str] = None) -> bool:
        """
        送出委託。
        - Paper: 直接設為 SUBMITTED
        - Live: 透過 broker_adapter.place_order() 送單
        """
        if order.status != OrderStatus.PENDING_SUBMIT:
            raise ValueError(f"Order {order.order_id} already submitted (status={order.status.value})")

        if self.mode == "live":
            return self._submit_live(order)
        else:
            return self._submit_paper(order, exchange_ordno)

    def _submit_live(self, order: Order) -> bool:
        if self.broker_adapter is None:
            raise RuntimeError("Live mode requires broker_adapter")

        # 2026-06-08 JVS Claw: MKP (範圍市價) translation for Shioaji broker.
        # Shioaji uses two dimensions: price_type (LMT/MKT/MKP) + order_type (ROD/IOC/FOK).
        # MKP requires IOC or FOK (not ROD). price=0 signals MKP/MKT to ShioajiClient.
        # Mapping:
        #   OrderType.MKP    → price=0, price_type=MKP, order_type=IOC
        #   OrderType.MARKET → price=0, price_type=MKT, order_type=IOC
        #   OrderType.LIMIT  → price=N, price_type=LMT, order_type=ROD
        if order.order_type == OrderType.MKP:
            order.price = 0  # MKP ignores price; ShioajiClient detects price==0 → MKP

        # [GSD] Compatibility bridge for ShioajiClient object-based placement
        if hasattr(self.broker_adapter, "place_order_object"):
            result = self.broker_adapter.place_order_object(order)
        else:
            result = self.broker_adapter.place_order(order)
            
        if result is None:
            order.status = OrderStatus.REJECTED
            order.reject_reason = "broker_api_failed"
            self._emit("on_reject", OrderEvent(
                order_id=order.order_id, status=OrderStatus.REJECTED,
                symbol=order.symbol, side=order.side,
                reason="broker_api_failed",
            ))
            return False

        self.attach_submission(
            order.order_id,
            broker_trade=result,
            broker_order_id=getattr(result, "id", None),
            seqno=getattr(result, "seqno", None),
            ordno=getattr(result, "ordno", None),
            raw_status="Submitted",
            source="broker_submit",
            reason="submit_live",
        )
        return True

    def _submit_paper(self, order: Order, exchange_ordno: Optional[str] = None) -> bool:
        self.attach_submission(
            order.order_id,
            broker_order_id=exchange_ordno or f"PAPER-{order.order_id}",
            ordno=exchange_ordno or f"PAPER-{order.order_id}",
            raw_status="Submitted",
            source="paper_submit",
            reason="submit_paper",
        )
        return True

    # ── Fill ──

    def apply_deal_fill(
        self,
        order_id: Optional[str],
        *,
        deal_id: Optional[str] = None,
        fill_price: float,
        fill_qty: int,
        fill_time: Optional[datetime] = None,
        exchange_fill_id: Optional[str] = None,
        broker_trade_id: Optional[str] = None,
        exchange_seq: Optional[str] = None,
        commission: float = 0.0,
        tax: float = 0.0,
        raw_payload: Optional[Dict[str, Any]] = None,
        broker_order_id: Optional[str] = None,
        ordno: Optional[str] = None,
        source: str = "",
        reason: str = "",
    ) -> Optional[Order]:
        order = self._resolve_order(order_id, broker_order_id=broker_order_id, ordno=ordno)
        if order is None:
            return None

        remaining = order.quantity - order.filled_quantity
        if fill_qty > remaining:
            raise ValueError(f"Fill qty {fill_qty} exceeds remaining {remaining} for {order.order_id}")
        if fill_qty <= 0:
            raise ValueError("Fill quantity must be positive")

        previous_status = order.status
        order.fill(
            fill_price,
            fill_qty,
            commission=commission,
            tax=tax,
            deal_id=deal_id,
            exchange_fill_id=exchange_fill_id,
            broker_trade_id=broker_trade_id,
            exchange_seq=exchange_seq,
            fill_time=fill_time,
        )
        self._record_audit(
            order,
            "deal_fill",
            source=source or "deal_fill",
            reason=reason,
            from_status=previous_status,
            to_status=order.status,
            payload=raw_payload,
            broker_order_id=broker_order_id or order.broker_order_id,
            seqno=order.seqno,
            ordno=ordno or order.ordno,
            deal_id=deal_id or broker_trade_id or exchange_fill_id or exchange_seq,
            fill_price=fill_price,
            fill_qty=fill_qty,
        )

        if order.status == OrderStatus.FILLED:
            if order.order_id in self.active_orders:
                self.completed.append(order)
                del self.active_orders[order.order_id]

        latest_fill = order.fills[-1] if order.fills else None
        self._emit("on_fill", OrderEvent(
            order_id=order.order_id,
            status=order.status,
            symbol=order.symbol,
            side=order.side,
            intent_id=order.intent_id,
            deal_id=latest_fill.deal_id if latest_fill else deal_id,
            broker_order_id=order.broker_order_id,
            seqno=order.seqno,
            ordno=order.ordno,
            fill_price=fill_price,
            fill_qty=fill_qty,
        ))
        return order

    def on_fill(
        self,
        order_id: str,
        fill_price: float,
        fill_qty: int,
        partial: bool,
        commission: float = 0.0,
        tax: float = 0.0,
    ) -> None:
        """
        成交回報（Live 從 Shioaji callback 呼叫 / Paper 從模擬器呼叫）。
        """
        order = self.active_orders.get(order_id)
        if order is None:
            return  # Silently ignore unknown orders

        remaining = order.quantity - order.filled_quantity
        if fill_qty > remaining:
            raise ValueError(f"Fill qty {fill_qty} exceeds remaining {remaining} for {order_id}")
        if fill_qty <= 0:
            raise ValueError("Fill quantity must be positive")

        self.apply_deal_fill(
            order_id,
            fill_price=fill_price,
            fill_qty=fill_qty,
            commission=commission,
            tax=tax,
        )

    # ── Release OCO Bracket (ADR-010) ──

    def submit_release_bracket(
        self,
        *,
        symbol_near: str,
        symbol_far: str,
        quantity: int = 1,
        side_near: OrderSide,
        side_far: OrderSide,
        strategy: str = "MTS_RELEASE_OCO",
        price_near: float | None = None,
        price_far: float | None = None,
    ) -> tuple[str, str]:
        """Submit a two-sided release OCO bracket.

        Creates and submits both near and far release orders with
        potentially opposite sides (e.g. SELL near, BUY far).
        Both order ids MUST be returned before any state is persisted
        (submit-before-commit invariant).

        Paper mode: creates two orders × submit.
        Live mode (future): broker-native OCO or two orders + auto-cancel.

        When price_near/price_far are provided, uses LIMIT order type
        so the paper_fill_sim only fills when the release threshold
        is reached.  Omit for MKP (range market) orders.

        Returns (near_order_id, far_order_id).
        Raises RuntimeError if either submission fails.
        """
        _order_type = OrderType.LIMIT if (price_near is not None and price_far is not None) else OrderType.MKP
        near_order = self.create_order(
            symbol=symbol_near, side=side_near, order_type=_order_type,
            quantity=quantity, strategy=strategy,
            price=price_near,
        )
        far_order = self.create_order(
            symbol=symbol_far, side=side_far, order_type=_order_type,
            quantity=quantity, strategy=strategy,
            price=price_far,
        )

        near_ok = self.submit(near_order)
        if not near_ok:
            # near failed → cancel far (if created) before raising
            self.reject(far_order.order_id, reason="bracket_near_failed")
            raise RuntimeError(f"Release bracket: near order {near_order.order_id} submission failed")

        far_ok = self.submit(far_order)
        if not far_ok:
            # near submitted, far failed → cancel near, raise
            self.cancel(near_order.order_id, reason="bracket_far_failed_rollback_near")
            raise RuntimeError(f"Release bracket: far order {far_order.order_id} submission failed")

        return (near_order.order_id, far_order.order_id)

    # ── Cancel (🛑 Gate) ──

    def cancel(self, order_id: str, reason: str = "", source: str = "") -> bool:
        """
        取消委託單。
        🛑 只允許 SUBMITTED / PARTIAL_FILLED / PENDING_SUBMIT / PRE_SUBMITTED
        FILLED / CANCELLED / REJECTED / EXPIRED → raises ValueError
        """
        order = self.active_orders.get(order_id)
        if order is None:
            # Check completed too
            for c in self.completed:
                if c.order_id == order_id:
                    raise ValueError(
                        f"Cannot cancel order {order_id}: status={c.status.value} (terminal)"
                    )
            raise KeyError(f"Order {order_id} not found")

        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                            OrderStatus.REJECTED, OrderStatus.EXPIRED):
            raise ValueError(
                f"Cannot cancel order {order_id}: status={order.status.value} (terminal)"
            )

        # Live: call broker cancel API
        if self.mode == "live" and order.exchange_order_id:
            if order.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED):
                if self.broker_adapter:
                    self.broker_adapter.cancel_order(order.exchange_order_id)

        previous_status = order.status
        order.cancel(reason=reason)
        if order_id in self.active_orders:
            self.completed.append(order)
            del self.active_orders[order_id]

        # Notify simulator to remove
        if self._simulator:
            self._simulator.remove(order_id)

        self._record_audit(
            order,
            "cancel",
            source=source or "cancel",
            reason=reason,
            from_status=previous_status,
            to_status=order.status,
        )
        self._emit("on_cancel", OrderEvent(
            order_id=order_id, status=OrderStatus.CANCELLED,
            symbol=order.symbol, side=order.side, reason=reason,
        ))
        return True

    # ── Reject ──

    def reject(self, order_id: str, reason: str, source: str = "") -> None:
        order = self.active_orders.get(order_id)
        if order is None:
            for c in self.completed:
                if c.order_id == order_id:
                    raise ValueError(f"Cannot reject order {order_id}: status={c.status.value} (terminal)")
            raise KeyError(f"Order {order_id} not found")

        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                            OrderStatus.REJECTED, OrderStatus.EXPIRED):
            raise ValueError(f"Cannot reject order {order_id}: status={order.status.value} (terminal)")

        previous_status = order.status
        order.reject(reason=reason)
        self.completed.append(order)
        del self.active_orders[order_id]
        self._record_audit(
            order,
            "reject",
            source=source or "reject",
            reason=reason,
            from_status=previous_status,
            to_status=order.status,
        )

        self._emit("on_reject", OrderEvent(
            order_id=order_id, status=OrderStatus.REJECTED,
            symbol=order.symbol, side=order.side, reason=reason,
        ))

    # ── Expire ──

    def expire(self, order_id: str, source: str = "", reason: str = "") -> None:
        order = self.active_orders.get(order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found")

        previous_status = order.status
        order.expire()
        self.completed.append(order)
        del self.active_orders[order_id]
        self._record_audit(
            order,
            "expire",
            source=source or "expire",
            reason=reason,
            from_status=previous_status,
            to_status=order.status,
        )

        self._emit("on_expire", OrderEvent(
            order_id=order_id, status=OrderStatus.EXPIRED,
            symbol=order.symbol, side=order.side,
        ))

    # ── Recovery (重啟恢復) ──

    def restore_orders(self, serialized_orders: Optional[list]) -> Dict[str, int]:
        restored = {"active": 0, "completed": 0, "failed": 0}
        if not serialized_orders:
            return restored

        for item in serialized_orders:
            try:
                order = item if isinstance(item, Order) else Order.from_dict(item)
            except Exception:
                restored["failed"] += 1
                continue

            self.active_orders.pop(order.order_id, None)
            self.completed = [existing for existing in self.completed if existing.order_id != order.order_id]
            if order.is_completed():
                self.completed.append(order)
                restored["completed"] += 1
            else:
                self.active_orders[order.order_id] = order
                restored["active"] += 1

        self.reindex_orders()
        return restored

    def reindex_orders(self) -> Dict[str, int]:
        """Scan existing orders for current session_date, set _next_id to max+1.

        2026-07-07 Hermes Agent: session_date-aware reindex.
        Supports both old format (ORD-XXXXXX) and new format (ORD-YYYYMMDD-XXXXXX).
        Scans in-memory orders AND the persisted orders JSON file so the counter
        survives PM2 restart (when in-memory state is empty but disk has history).
        On session boundary, _session_date changes → finds no matching orders → resets to 1.
        """
        max_order_index = 0

        def _scan_id(oid: str) -> int:
            if not oid or not oid.startswith("ORD-"):
                return 0
            suffix = oid[4:]
            parts = suffix.split("-", 1)
            if len(parts) == 2:
                date_part, counter_part = parts
                if date_part == self._session_date and counter_part.isdigit():
                    return int(counter_part)
            elif suffix.isdigit():
                return int(suffix)
            return 0

        for order in list(self.active_orders.values()) + self.completed:
            max_order_index = max(max_order_index, _scan_id(order.order_id))

        # 2026-07-07 Hermes Agent: also scan persisted orders file.
        # After PM2 restart the in-memory collections are empty, but the
        # orders file holds the full history for this session.
        try:
            import json, os as _os
            _orders_file = f"exports/trades/TMF_{self._session_date}_orders.json"
            if _os.path.exists(_orders_file):
                with open(_orders_file) as _of:
                    _orders_data = json.load(_of)
                if isinstance(_orders_data, list):
                    for _entry in _orders_data:
                        if isinstance(_entry, dict):
                            max_order_index = max(
                                max_order_index,
                                _scan_id(_entry.get("order_id", "")),
                            )
        except Exception:
            pass

        self._next_id = max(self._next_id, max_order_index + 1)
        return {"active": len(self.active_orders), "completed": len(self.completed)}

    def clear_session_orders(self) -> None:
        """Clear all active and completed orders for the current session and reset next ID.

        2026-07-07 Gemini CLI / Hermes Agent: Reset session-date-aware state and next_id.
        """
        self.active_orders.clear()
        self.completed = []
        self._next_id = 1
        try:
            from core.date_utils import get_session_date_str
            self._session_date = get_session_date_str()
        except Exception:
            self._session_date = datetime.now().strftime("%Y%m%d")

    def reconcile_trade_snapshot(
        self,
        order_id: Optional[str] = None,
        *,
        trade=None,
        broker_order_id: Optional[str] = None,
        ordno: Optional[str] = None,
        source: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        broker_order_id = broker_order_id or self._extract_value(trade, "id", "broker_order_id", "exchange_order_id")
        ordno = ordno or self._extract_value(trade, "ordno", "exchange_order_id")
        seqno = self._extract_value(trade, "seqno")
        order = self._resolve_order(order_id, broker_order_id=broker_order_id, seqno=seqno, ordno=ordno)
        if order is None:
            return {
                "matched": False,
                "action": "unmatched_snapshot",
                "order_id": None,
                "fills_added": 0,
            }

        raw_status = self._extract_value(getattr(trade, "status", None), "status") or self._extract_value(trade, "status")
        normalized_status = self._normalize_raw_status(raw_status)
        deals = self._extract_value(getattr(trade, "status", None), "deals") or self._extract_value(trade, "deals") or []
        if broker_order_id or ordno or seqno:
            self.attach_submission(
                order.order_id,
                broker_trade=trade,
                broker_order_id=broker_order_id,
                seqno=seqno,
                ordno=ordno,
                raw_status="Submitted",
                source=source or "reconcile",
                reason=reason,
            )

        if raw_status is not None and not (normalized_status == OrderStatus.FILLED and deals):
            self.apply_order_update(
                order.order_id,
                raw_status=raw_status,
                reason=reason,
                raw_payload=self._payload_to_dict(trade),
                broker_order_id=broker_order_id,
                seqno=seqno,
                ordno=ordno,
                source=source or "reconcile",
            )

        fills_added = 0
        for deal in deals:
            deal_id = self._extract_value(deal, "deal_id", "trade_id", "fill_id")
            broker_trade_id = self._extract_value(deal, "broker_trade_id", "trade_id")
            exchange_fill_id = self._extract_value(deal, "exchange_fill_id", "fill_id")
            exchange_seq = self._extract_value(deal, "exchange_seq")
            if self._has_fill_identity(
                order,
                deal_id=deal_id,
                broker_trade_id=broker_trade_id,
                exchange_fill_id=exchange_fill_id,
                exchange_seq=exchange_seq,
            ):
                continue

            fill_price = float(self._extract_value(deal, "price", "avg_price") or 0)
            fill_qty = int(self._extract_value(deal, "quantity", "qty") or 0)
            if fill_qty <= 0:
                continue

            self.apply_deal_fill(
                order.order_id,
                deal_id=deal_id,
                fill_price=fill_price,
                fill_qty=fill_qty,
                exchange_fill_id=exchange_fill_id,
                broker_trade_id=broker_trade_id,
                exchange_seq=exchange_seq,
                raw_payload=self._payload_to_dict(deal),
                broker_order_id=broker_order_id,
                ordno=ordno or self._extract_value(deal, "ordno"),
                source=source or "reconcile",
                reason=reason,
            )
            fills_added += 1

        self._record_audit(
            order,
            "reconcile",
            source=source or "reconcile",
            reason=reason,
            to_status=order.status,
            raw_status=raw_status,
            payload=trade,
            broker_order_id=broker_order_id,
            seqno=seqno,
            ordno=ordno,
        )
        return {
            "matched": True,
            "action": "reconciled",
            "order_id": order.order_id,
            "fills_added": fills_added,
        }

    def _normalize_combo_deals(self, deals) -> Dict[str, List[Dict[str, Any]]]:
        if not deals:
            return {}
        if isinstance(deals, dict):
            normalized = {}
            for leg_code, leg_deals in deals.items():
                if isinstance(leg_deals, list):
                    normalized[str(leg_code)] = [self._payload_to_dict(deal) or {} for deal in leg_deals]
            return normalized
        return {}

    def _combo_filled_quantity(self, combo_trade) -> int:
        status = getattr(combo_trade, "status", None)
        deals = self._normalize_combo_deals(
            self._extract_value(status, "deals") or self._extract_value(combo_trade, "deals")
        )
        if not deals:
            return 0

        per_leg_qty = []
        for leg_deals in deals.values():
            leg_qty = 0
            for deal in leg_deals:
                leg_qty += int(self._extract_value(deal, "quantity", "qty") or 1)
            per_leg_qty.append(leg_qty)
        return min(per_leg_qty) if per_leg_qty else 0

    def _combo_fill_price(self, combo_trade, order: Order) -> float:
        status = getattr(combo_trade, "status", None)
        price = self._extract_value(status, "price", "avg_price")
        if price is None:
            price = self._extract_value(combo_trade, "price", "avg_price")
        if price is None:
            price = order.price
        return float(price or 0.0)

    def _build_combo_fill_identity(self, combo_trade, net_filled_qty: int) -> str:
        status = getattr(combo_trade, "status", None)
        deals = self._normalize_combo_deals(
            self._extract_value(status, "deals") or self._extract_value(combo_trade, "deals")
        )
        tokens = []
        for leg_code in sorted(deals):
            for deal in deals[leg_code]:
                token = (
                    self._extract_value(deal, "deal_id", "trade_id", "fill_id", "seq", "exchange_seq")
                    or f"{leg_code}:{self._extract_value(deal, 'ordno') or ''}:{self._extract_value(deal, 'quantity', 'qty') or 1}"
                )
                tokens.append(str(token))
        ordno = self._extract_value(combo_trade, "ordno", "exchange_order_id") or "combo"
        joined = "|".join(sorted(tokens)) or f"net={net_filled_qty}"
        return f"combo:{ordno}:{joined}"

    def _ensure_recovered_combo_order(
        self,
        combo_trade,
        *,
        broker_order_id: Optional[str] = None,
        seqno: Optional[str] = None,
        ordno: Optional[str] = None,
    ) -> Order:
        action = str(self._extract_value(combo_trade, "action") or "Sell").strip().lower()
        side = OrderSide.BUY if action == "buy" else OrderSide.SELL
        quantity = int(
            self._extract_value(getattr(combo_trade, "status", None), "quantity")
            or self._extract_value(combo_trade, "quantity")
            or 1
        )
        price = float(
            self._extract_value(getattr(combo_trade, "status", None), "price", "avg_price")
            or self._extract_value(combo_trade, "price", "avg_price")
            or 0.0
        )
        recovery_key = ordno or broker_order_id or f"{self._next_id:06d}"
        order = Order(
            symbol="TXO-COMBO",
            side=side,
            order_type=OrderType.LIMIT,
            quantity=max(1, quantity),
            price=price,
            order_id=f"RECOV-{recovery_key}",
            truth_source="broker_combo",
            combo_strategy=str(self._extract_value(combo_trade, "strategy") or ""),
        )
        order.broker_order_id = broker_order_id
        order.seqno = seqno
        order.ordno = ordno
        order.exchange_order_id = ordno or broker_order_id
        self.active_orders[order.order_id] = order
        self.reindex_orders()
        return order

    def reconcile_combo_trade_snapshot(
        self,
        order_id: Optional[str] = None,
        *,
        combo_trade=None,
        broker_order_id: Optional[str] = None,
        ordno: Optional[str] = None,
        source: str = "",
        reason: str = "",
        create_if_missing: bool = False,
    ) -> Dict[str, Any]:
        broker_order_id = broker_order_id or self._extract_value(combo_trade, "id", "broker_order_id", "exchange_order_id")
        ordno = ordno or self._extract_value(combo_trade, "ordno", "exchange_order_id")
        seqno = self._extract_value(combo_trade, "seqno")
        order = self._resolve_order(order_id, broker_order_id=broker_order_id, seqno=seqno, ordno=ordno)
        created = False
        if order is None and create_if_missing:
            order = self._ensure_recovered_combo_order(
                combo_trade,
                broker_order_id=broker_order_id,
                seqno=seqno,
                ordno=ordno,
            )
            created = True
        if order is None:
            return {
                "matched": False,
                "action": "unmatched_combo_snapshot",
                "order_id": None,
                "fills_added": 0,
                "created": False,
            }

        raw_status = self._extract_value(getattr(combo_trade, "status", None), "status") or self._extract_value(combo_trade, "status")
        if (broker_order_id or ordno or seqno) and not order.is_completed():
            self.attach_submission(
                order.order_id,
                broker_trade=combo_trade,
                broker_order_id=broker_order_id,
                seqno=seqno,
                ordno=ordno,
                raw_status="Submitted",
                source=source or "combo_reconcile",
                reason=reason,
            )

        normalized_status = self._normalize_raw_status(raw_status)
        if raw_status is not None:
            self.apply_order_update(
                order.order_id,
                raw_status=raw_status,
                reason=reason,
                raw_payload=self._payload_to_dict(combo_trade),
                broker_order_id=broker_order_id,
                seqno=seqno,
                ordno=ordno,
                source=source or "combo_reconcile",
            )

        fills_added = 0
        net_filled_qty = self._combo_filled_quantity(combo_trade) if normalized_status in (OrderStatus.PARTIAL_FILLED, OrderStatus.FILLED) else 0
        fill_delta = max(0, net_filled_qty - order.filled_quantity)
        if fill_delta > 0:
            fill_identity = self._build_combo_fill_identity(combo_trade, net_filled_qty)
            if not self._has_fill_identity(
                order,
                deal_id=fill_identity,
                broker_trade_id=fill_identity,
                exchange_fill_id=fill_identity,
            ):
                self.apply_deal_fill(
                    order.order_id,
                    deal_id=fill_identity,
                    fill_price=self._combo_fill_price(combo_trade, order),
                    fill_qty=fill_delta,
                    exchange_fill_id=fill_identity,
                    broker_trade_id=fill_identity,
                    raw_payload=self._payload_to_dict(combo_trade),
                    broker_order_id=broker_order_id,
                    ordno=ordno,
                    source=source or "combo_reconcile",
                    reason=reason,
                )
                fills_added += 1

        self._record_audit(
            order,
            "combo_reconcile",
            source=source or "combo_reconcile",
            reason=reason,
            to_status=order.status,
            raw_status=raw_status,
            payload=combo_trade,
            broker_order_id=broker_order_id,
            seqno=seqno,
            ordno=ordno,
        )
        return {
            "matched": True,
            "action": "combo_reconciled",
            "order_id": order.order_id,
            "fills_added": fills_added,
            "created": created,
            "status": order.status.value,
        }

    def reconcile_broker_state(
        self,
        open_orders: Optional[list] = None,
        filled_trades: Optional[list] = None,
        source: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        reconciled = []
        unmatched = []
        for trade in (open_orders or []) + (filled_trades or []):
            result = self.reconcile_trade_snapshot(
                trade=trade,
                source=source,
                reason=reason,
            )
            if result["matched"]:
                reconciled.append(result)
            else:
                unmatched.append(result)
        return {"reconciled": reconciled, "unmatched": unmatched}

    def recover_from_api(
        self,
        filled_trades: Optional[list] = None,
        open_orders: Optional[list] = None,
        combo_trades: Optional[list] = None,
        source: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        """
        重啟後從 API 重建訂單狀態表。
        應在啟動時立即呼叫 api.list_trades() + api.list_open_orders()。
        """
        recovered = {"filled": 0, "open": 0, "failed": 0}

        if combo_trades:
            for combo_trade in combo_trades:
                result = self.reconcile_combo_trade_snapshot(
                    combo_trade=combo_trade,
                    source=source or "api_recovery",
                    reason=reason or "recover_from_api",
                    create_if_missing=True,
                )
                if not result["matched"]:
                    recovered["failed"] += 1
                    continue
                order = self._resolve_order(result["order_id"])
                if order is None:
                    order = next((candidate for candidate in self.completed if candidate.order_id == result["order_id"]), None)
                if order is None:
                    recovered["failed"] += 1
                elif order.is_completed() and order.status == OrderStatus.FILLED:
                    recovered["filled"] += 1
                elif order.is_active():
                    recovered["open"] += 1

        # Rebuild filled orders
        if filled_trades:
            for trade in filled_trades:
                ordno = getattr(trade, "ordno", None) or getattr(trade, "exchange_order_id", None)
                if ordno is None:
                    recovered["failed"] += 1
                    continue
                order = Order(
                    symbol=getattr(trade, "symbol", "UNKNOWN"),
                    side=OrderSide.BUY if getattr(trade, "action", "Buy") == "Buy" else OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=getattr(trade, "quantity", 0),
                    price=getattr(trade, "price", 0),
                    order_id=f"RECOV-{ordno}",
                )
                order.status = OrderStatus.FILLED
                order.exchange_order_id = ordno
                order.broker_order_id = ordno
                order.ordno = ordno
                order.filled_quantity = getattr(trade, "quantity", 0)
                order.avg_fill_price = getattr(trade, "price", 0)
                order.filled_at = datetime.now()
                self._record_audit(
                    order,
                    "recovery",
                    source=source or "api_recovery",
                    reason=reason or "recover_from_api",
                    to_status=order.status,
                    payload=trade,
                    broker_order_id=order.broker_order_id,
                    ordno=order.ordno,
                )
                self.completed.append(order)
                recovered["filled"] += 1

        # Rebuild open (pending) orders
        if open_orders:
            for oo in open_orders:
                ordno = getattr(oo, "ordno", None) or getattr(oo, "exchange_order_id", None)
                if ordno is None:
                    recovered["failed"] += 1
                    continue
                order = Order(
                    symbol=getattr(oo, "symbol", "UNKNOWN"),
                    side=OrderSide.BUY if getattr(oo, "action", "Buy") == "Buy" else OrderSide.SELL,
                    order_type=OrderType.MARKET,
                    quantity=getattr(oo, "quantity", 0),
                    price=getattr(oo, "price", 0),
                    order_id=f"RECOV-{ordno}",
                )
                order.status = OrderStatus.SUBMITTED
                order.exchange_order_id = ordno
                order.broker_order_id = ordno
                order.ordno = ordno
                order.submitted_at = datetime.now()
                self._record_audit(
                    order,
                    "recovery",
                    source=source or "api_recovery",
                    reason=reason or "recover_from_api",
                    to_status=order.status,
                    payload=oo,
                    broker_order_id=order.broker_order_id,
                    ordno=order.ordno,
                )
                self.active_orders[order.order_id] = order
                recovered["open"] += 1

        self.reindex_orders()
        return recovered

    # ── Query ──

    def get_pending(self) -> List[Order]:
        """取得所有尚未完成的委託單（SUBMITTED / PARTIAL_FILLED / PRE_SUBMITTED）"""
        return [
            o for o in self.active_orders.values()
            if o.status in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED,
                           OrderStatus.PENDING_SUBMIT, OrderStatus.PRE_SUBMITTED)
        ]

    def get_completed(self) -> List[Order]:
        """取得所有已完成的委託單"""
        return list(self.completed)

    def get_orders_by_symbol(self, symbol: str) -> List[Order]:
        """依商品代碼查詢"""
        all_orders = list(self.active_orders.values()) + self.completed
        return [o for o in all_orders if o.symbol == symbol]

    def get_order(self, order_id: str) -> Optional[Order]:
        return self.active_orders.get(order_id)

    # ── Callback ──

    def register_callback(self, event_type: str, callback: Callable) -> None:
        if event_type in self._callbacks:
            self._callbacks[event_type].append(callback)

    def _emit(self, event_type: str, event: OrderEvent) -> None:
        for cb in self._callbacks.get(event_type, []):
            try:
                cb(event)
            except Exception:
                pass  # Callback errors should not break the manager

    def __repr__(self) -> str:
        return (f"OrderManager(mode={self.mode!r}, "
                f"active={len(self.active_orders)}, completed={len(self.completed)})")
