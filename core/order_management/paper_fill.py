"""
Paper Fill Simulator (紙本撮合引擎)

在 Paper Mode 下模擬真實的委託→撮合流程：
- 市價單 (MARKET)：下一筆 tick 的 close 價格即成交
- 限價單 (LIMIT)：價格穿過限價即成交
  - 買入: low <= limit_price → 成交（買得更便宜）
  - 賣出: high >= limit_price → 成交（賣得更好）
- 部分成交：大單 (≥2 lots) 每次 tick 只成交 1 lot，模擬流動性限制

⚡ 非同步設計: process_tick() 不阻塞，被 monitor 主循環呼叫。
"""
from __future__ import annotations

import math
from typing import Dict, List, TYPE_CHECKING

if TYPE_CHECKING:
    from core.order_management.order_manager import OrderManager
    from core.order_management.order import Order


class PaperFillSimulator:
    """
    Paper Mode 撮合模擬器。

    註冊已提交的委託單，每個 tick 檢查是否符合成交條件。
    """

    def __init__(self, order_mgr: "OrderManager"):
        self._order_mgr = order_mgr
        self._pending_orders: Dict[str, "Order"] = {}  # order_id → Order

    def register(self, order: "Order") -> None:
        """
        註冊委託單進入模擬撮合。
        只有 SUBMITTED / PRE_SUBMITTED 狀態的訂單才能被註冊。
        """
        from core.order_management.order import OrderStatus

        if order.status not in (OrderStatus.SUBMITTED, OrderStatus.PRE_SUBMITTED):
            return
        self._pending_orders[order.order_id] = order

    def process_tick(self, tick) -> None:
        """
        處理一筆 tick 數據，檢查是否有委託單符合成交條件。
        被 monitor 的主循環定期呼叫（通常每 5-15 秒）。

        Args:
            tick: Shioaji tick object with attributes:
                  datetime, open, high, low, close, volume
        """
        from core.order_management.order import OrderType, OrderSide

        # Snapshot keys to avoid modification during iteration
        order_ids = list(self._pending_orders.keys())

        for order_id in order_ids:
            order = self._pending_orders.get(order_id)
            if order is None:
                continue

            # Symbol Guard: ensure tick matches order symbol
            tick_symbol = getattr(tick, "symbol", getattr(tick, "code", None))
            if tick_symbol and tick_symbol != order.symbol:
                continue

            remaining = order.quantity - order.filled_quantity
            if remaining <= 0:
                continue

            # Determine fill price and whether to fill
            fill_price = None

            if order.order_type == OrderType.MARKET:
                # 市價單：以 close 成交
                fill_price = float(tick.close)

            elif order.order_type == OrderType.LIMIT and order.price is not None:
                # 限價單：檢查價格是否穿過限價
                low = float(tick.low)
                high = float(tick.high)
                close = float(tick.close)
                limit = order.price

                if order.side == OrderSide.BUY:
                    # 買入: low <= limit → 可以買得比限價更好
                    if low <= limit:
                        # Fill at close if close <= limit, otherwise at limit
                        fill_price = min(close, limit)
                else:  # SELL
                    # 賣出: high >= limit → 可以賣得比限價更好
                    if high >= limit:
                        fill_price = max(close, limit)

            if fill_price is not None:
                # Determine fill quantity (partial fill for large orders)
                if remaining >= 2:
                    # Partial fill: 1 lot per tick (simulate liquidity constraint)
                    fill_qty = 1
                else:
                    fill_qty = remaining

                self._order_mgr.on_fill(
                    order_id=order.order_id,
                    fill_price=fill_price,
                    fill_qty=fill_qty,
                    partial=(fill_qty < remaining),
                )

                # If still has remaining, keep in pending; otherwise remove
                if order_id in self._pending_orders:
                    order = self._pending_orders.get(order_id)
                    if order is None or order.status in ("filled", "FILLED"):
                        self._pending_orders.pop(order_id, None)
                    elif order is not None:
                        # Check if filled via on_fill
                        if hasattr(order, "filled_quantity") and order.filled_quantity >= order.quantity:
                            self._pending_orders.pop(order_id, None)

    def remove(self, order_id: str) -> None:
        """手動移除委託單（用於 cancel/reject/expire）"""
        self._pending_orders.pop(order_id, None)

    def get_pending_count(self) -> int:
        return len(self._pending_orders)

    def __repr__(self) -> str:
        return f"PaperFillSimulator(pending={len(self._pending_orders)})"
