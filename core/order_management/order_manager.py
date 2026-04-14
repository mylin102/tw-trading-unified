"""
統一委託單管理器 (OrderManager)
Paper / Live 共用同一套狀態機。

🛑 刪除單限制: 只允許 SUBMITTED / PARTIAL_FILLED 狀態取消。
⚡ 重啟恢復: 呼叫 recover_from_api() 重建狀態表。
"""
from __future__ import annotations

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
    fill_price: Optional[float] = None
    fill_qty: int = 0
    reason: str = ""
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
        self._next_id = 1
        self.broker_adapter = broker_adapter

        # Callback 系統
        self._callbacks: Dict[str, List[Callable]] = {
            "on_fill": [],
            "on_cancel": [],
            "on_reject": [],
            "on_expire": [],
        }

        # Simulator reference (for paper mode cleanup)
        self._simulator = None

    def set_simulator(self, sim) -> None:
        """連結 PaperFillSimulator，用於 cancel/reject 時同步移除"""
        self._simulator = sim

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
    ) -> Order:
        """建立新委託單，狀態→ PENDING_SUBMIT"""
        order_id = f"ORD-{self._next_id:06d}"
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
        )
        self.active_orders[order_id] = order
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

        order.status = OrderStatus.SUBMITTED
        order.exchange_order_id = result.ordno if hasattr(result, "ordno") else str(id(result))
        order.submitted_at = datetime.now()
        return True

    def _submit_paper(self, order: Order, exchange_ordno: Optional[str] = None) -> bool:
        order.status = OrderStatus.SUBMITTED
        order.exchange_order_id = exchange_ordno or f"PAPER-{order.order_id}"
        order.submitted_at = datetime.now()
        return True

    # ── Fill ──

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

        order.fill(fill_price, fill_qty, commission=commission, tax=tax)

        if partial or order.status == OrderStatus.PARTIAL_FILLED:
            order.status = OrderStatus.PARTIAL_FILLED
        else:
            order.status = OrderStatus.FILLED
            order.filled_at = datetime.now()
            self.completed.append(order)
            del self.active_orders[order_id]

        self._emit("on_fill", OrderEvent(
            order_id=order_id, status=order.status,
            symbol=order.symbol, side=order.side,
            fill_price=fill_price, fill_qty=fill_qty,
        ))

    # ── Cancel (🛑 Gate) ──

    def cancel(self, order_id: str, reason: str = "") -> bool:
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

        order.cancel(reason=reason)
        if order_id in self.active_orders:
            self.completed.append(order)
            del self.active_orders[order_id]

        # Notify simulator to remove
        if self._simulator:
            self._simulator.remove(order_id)

        self._emit("on_cancel", OrderEvent(
            order_id=order_id, status=OrderStatus.CANCELLED,
            symbol=order.symbol, side=order.side, reason=reason,
        ))
        return True

    # ── Reject ──

    def reject(self, order_id: str, reason: str) -> None:
        order = self.active_orders.get(order_id)
        if order is None:
            for c in self.completed:
                if c.order_id == order_id:
                    raise ValueError(f"Cannot reject order {order_id}: status={c.status.value} (terminal)")
            raise KeyError(f"Order {order_id} not found")

        if order.status in (OrderStatus.FILLED, OrderStatus.CANCELLED,
                            OrderStatus.REJECTED, OrderStatus.EXPIRED):
            raise ValueError(f"Cannot reject order {order_id}: status={order.status.value} (terminal)")

        order.reject(reason=reason)
        self.completed.append(order)
        del self.active_orders[order_id]

        self._emit("on_reject", OrderEvent(
            order_id=order_id, status=OrderStatus.REJECTED,
            symbol=order.symbol, side=order.side, reason=reason,
        ))

    # ── Expire ──

    def expire(self, order_id: str) -> None:
        order = self.active_orders.get(order_id)
        if order is None:
            raise KeyError(f"Order {order_id} not found")

        order.expire()
        self.completed.append(order)
        del self.active_orders[order_id]

        self._emit("on_expire", OrderEvent(
            order_id=order_id, status=OrderStatus.EXPIRED,
            symbol=order.symbol, side=order.side,
        ))

    # ── Recovery (重啟恢復) ──

    def recover_from_api(
        self,
        filled_trades: Optional[list] = None,
        open_orders: Optional[list] = None,
    ) -> Dict[str, Any]:
        """
        重啟後從 API 重建訂單狀態表。
        應在啟動時立即呼叫 api.list_trades() + api.list_open_orders()。
        """
        recovered = {"filled": 0, "open": 0, "failed": 0}

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
                order.filled_quantity = getattr(trade, "quantity", 0)
                order.avg_fill_price = getattr(trade, "price", 0)
                order.filled_at = datetime.now()
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
                order.submitted_at = datetime.now()
                self.active_orders[order.order_id] = order
                recovered["open"] += 1

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
