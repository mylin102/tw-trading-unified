"""
委託單核心類別
定義委託單狀態機、類型、方向等基本元素
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, Any, List
import json


class OrderStatus(Enum):
    """委託單狀態枚舉 (對應 Shioaji OrderState + 生命週期)"""
    PENDING_SUBMIT = "pending_submit"    # 待傳送（剛建立，尚未送出）
    PRE_SUBMITTED = "pre_submitted"      # 預約單（盤前/盤後預約，尚未進入撮合）
    SUBMITTED = "submitted"              # 已提交，排隊撮合中
    PARTIAL_FILLED = "partial_filled"    # 部分成交
    FILLED = "filled"                    # 完全成交（終端狀態）
    CANCELLED = "cancelled"              # 已取消（終端狀態）
    REJECTED = "rejected"                # 退單/失敗（終端狀態）
    EXPIRED = "expired"                  # 過期未成交（終端狀態）


class OrderType(Enum):
    """委託單類型枚舉"""
    MARKET = "market"            # 市價單
    LIMIT = "limit"              # 限價單
    STOP = "stop"                # 停損單
    STOP_LIMIT = "stop_limit"    # 停損限價單


class OrderSide(Enum):
    """委託單方向枚舉"""
    BUY = "buy"                  # 買入
    SELL = "sell"                # 賣出


class Order:
    """委託單類別，管理單一委託單的生命週期"""
    
    def __init__(
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
        order_id: Optional[str] = None,
        parent_order_id: Optional[str] = None,
    ):
        """
        初始化委託單
        
        Args:
            symbol: 商品代碼 (e.g., "TXF", "MXF")
            side: 買賣方向
            order_type: 委託單類型
            quantity: 數量
            price: 限價價格 (限價單需要)
            stop_price: 停損價格 (停損單需要)
            strategy: 策略名稱
            account: 帳戶名稱
            comment: 備註
            order_id: 委託單ID (自動生成如果為None)
            parent_order_id: 父委託單ID (用於分批下單)
        """
        self.order_id = order_id or uuid.uuid4().hex[:8]
        self.symbol = symbol
        self.side = side
        self.order_type = order_type
        self.quantity = quantity
        self.price = price
        self.stop_price = stop_price
        self.strategy = strategy
        self.account = account
        self.comment = comment
        self.parent_order_id = parent_order_id
        
        # 狀態追蹤
        self.status = OrderStatus.PENDING_SUBMIT
        self.filled_quantity = 0
        self.avg_fill_price = 0.0
        self.commission = 0.0
        self.tax = 0.0
        self.total_fee = 0.0
        
        # 時間戳記
        self.created_at = datetime.now()
        self.submitted_at: Optional[datetime] = None
        self.filled_at: Optional[datetime] = None
        self.cancelled_at: Optional[datetime] = None
        self.rejected_at: Optional[datetime] = None
        self.expired_at: Optional[datetime] = None
        self.updated_at = self.created_at
        
        # 執行細節
        self.exchange_order_id: Optional[str] = None
        self.reject_reason: Optional[str] = None
        self.cancel_reason: Optional[str] = None
        
        # 執行品質指標
        self.slippage = 0.0
        self.fill_time_ms: Optional[int] = None
        
    def submit(self, exchange_order_id: str) -> None:
        """提交委託單到交易所"""
        if self.status != OrderStatus.PENDING_SUBMIT:
            raise ValueError(f"Cannot submit order in {self.status.value} state")

        self.status = OrderStatus.SUBMITTED
        self.submitted_at = datetime.now()
        self.exchange_order_id = exchange_order_id
        self.updated_at = self.submitted_at
        
    def fill(self, fill_price: float, fill_quantity: int, 
             commission: float = 0.0, tax: float = 0.0) -> None:
        """
        成交委託單（部分或全部）

        Args:
            fill_price: 成交價格
            fill_quantity: 成交數量
            commission: 手續費
            tax: 交易稅
        """
        if self.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED,
                               OrderStatus.PRE_SUBMITTED):
            raise ValueError(f"Cannot fill order in {self.status.value} state")
        
        if fill_quantity <= 0:
            raise ValueError("Fill quantity must be positive")
        
        if fill_quantity > (self.quantity - self.filled_quantity):
            raise ValueError(f"Fill quantity {fill_quantity} exceeds remaining quantity {self.quantity - self.filled_quantity}")
        
        # 更新成交資訊
        old_filled = self.filled_quantity
        self.filled_quantity += fill_quantity
        
        # 計算平均成交價
        if self.filled_quantity > 0:
            self.avg_fill_price = (
                (old_filled * self.avg_fill_price + fill_quantity * fill_price) 
                / self.filled_quantity
            )
        
        # 更新費用
        self.commission += commission
        self.tax += tax
        self.total_fee = self.commission + self.tax
        
        # 更新狀態
        if self.filled_quantity == self.quantity:
            self.status = OrderStatus.FILLED
            self.filled_at = datetime.now()
        else:
            self.status = OrderStatus.PARTIAL_FILLED
        
        # 計算滑價（限價單才有意義）
        if self.order_type == OrderType.LIMIT and self.price:
            if self.side == OrderSide.BUY:
                self.slippage = fill_price - self.price  # 買入：正數表示比限價貴
            else:
                self.slippage = self.price - fill_price  # 賣出：正數表示比限價便宜
        
        # 計算成交時間
        if self.submitted_at:
            fill_time = datetime.now() - self.submitted_at
            self.fill_time_ms = int(fill_time.total_seconds() * 1000)
        
        self.updated_at = datetime.now()
        
    def cancel(self, reason: str = "") -> None:
        """
        取消委託單
        🛑 只允許在 SUBMITTED 或 PARTIAL_FILLED 狀態時取消
        """
        if self.status not in (OrderStatus.SUBMITTED, OrderStatus.PARTIAL_FILLED,
                               OrderStatus.PENDING_SUBMIT, OrderStatus.PRE_SUBMITTED):
            raise ValueError(f"Cannot cancel order in {self.status.value} state (terminal)")

        self.status = OrderStatus.CANCELLED
        self.cancelled_at = datetime.now()
        self.cancel_reason = reason
        self.updated_at = self.cancelled_at

    def reject(self, reason: str) -> None:
        """拒絕委託單"""
        if self.status not in (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED,
                               OrderStatus.PRE_SUBMITTED):
            raise ValueError(f"Cannot reject order in {self.status.value} state (terminal)")
        
        self.status = OrderStatus.REJECTED
        self.rejected_at = datetime.now()
        self.reject_reason = reason
        self.updated_at = self.rejected_at
        
    def expire(self) -> None:
        """委託單過期（收盤未成交）"""
        if self.status not in (OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED,
                               OrderStatus.PRE_SUBMITTED):
            raise ValueError(f"Cannot expire order in {self.status.value} state (terminal)")
        
        self.status = OrderStatus.EXPIRED
        self.expired_at = datetime.now()
        self.updated_at = self.expired_at
        
    def is_active(self) -> bool:
        """檢查委託單是否仍活躍（可被成交或取消）"""
        return self.status in (
            OrderStatus.PENDING_SUBMIT,
            OrderStatus.PRE_SUBMITTED,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIAL_FILLED,
        )

    def is_completed(self) -> bool:
        """檢查委託單是否已完成（終端狀態）"""
        return self.status in (
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        )
        
    def get_remaining_quantity(self) -> int:
        """取得剩餘數量"""
        return self.quantity - self.filled_quantity
        
    def to_dict(self) -> Dict[str, Any]:
        """轉換為字典格式"""
        return {
            "order_id": self.order_id,
            "symbol": self.symbol,
            "side": self.side.value,
            "order_type": self.order_type.value,
            "quantity": self.quantity,
            "filled_quantity": self.filled_quantity,
            "remaining_quantity": self.get_remaining_quantity(),
            "price": self.price,
            "stop_price": self.stop_price,
            "avg_fill_price": self.avg_fill_price,
            "status": self.status.value,
            "strategy": self.strategy,
            "account": self.account,
            "comment": self.comment,
            "commission": self.commission,
            "tax": self.tax,
            "total_fee": self.total_fee,
            "slippage": self.slippage,
            "fill_time_ms": self.fill_time_ms,
            "exchange_order_id": self.exchange_order_id,
            "reject_reason": self.reject_reason,
            "cancel_reason": self.cancel_reason,
            "parent_order_id": self.parent_order_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "filled_at": self.filled_at.isoformat() if self.filled_at else None,
            "cancelled_at": self.cancelled_at.isoformat() if self.cancelled_at else None,
            "rejected_at": self.rejected_at.isoformat() if self.rejected_at else None,
            "expired_at": self.expired_at.isoformat() if self.expired_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
        
    def to_json(self) -> str:
        """轉換為JSON格式"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Order":
        """從字典創建Order實例"""
        # 創建基本實例
        order = cls(
            symbol=data["symbol"],
            side=OrderSide(data["side"]),
            order_type=OrderType(data["order_type"]),
            quantity=data["quantity"],
            price=data.get("price"),
            stop_price=data.get("stop_price"),
            strategy=data.get("strategy", ""),
            account=data.get("account", ""),
            comment=data.get("comment", ""),
            order_id=data["order_id"],
            parent_order_id=data.get("parent_order_id"),
        )
        
        # 恢復狀態
        order.status = OrderStatus(data["status"])
        order.filled_quantity = data["filled_quantity"]
        order.avg_fill_price = data.get("avg_fill_price", 0.0)
        order.commission = data.get("commission", 0.0)
        order.tax = data.get("tax", 0.0)
        order.total_fee = data.get("total_fee", 0.0)
        order.slippage = data.get("slippage", 0.0)
        order.fill_time_ms = data.get("fill_time_ms")
        order.exchange_order_id = data.get("exchange_order_id")
        order.reject_reason = data.get("reject_reason")
        order.cancel_reason = data.get("cancel_reason")
        
        # 恢復時間戳記
        def parse_datetime(dt_str):
            if not dt_str:
                return None
            return datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        
        order.created_at = parse_datetime(data.get("created_at"))
        order.submitted_at = parse_datetime(data.get("submitted_at"))
        order.filled_at = parse_datetime(data.get("filled_at"))
        order.cancelled_at = parse_datetime(data.get("cancelled_at"))
        order.rejected_at = parse_datetime(data.get("rejected_at"))
        order.expired_at = parse_datetime(data.get("expired_at"))
        order.updated_at = parse_datetime(data.get("updated_at")) or order.created_at
        
        return order
        
    def __repr__(self) -> str:
        return f"Order({self.order_id}, {self.symbol}, {self.side.value}, {self.status.value}, {self.filled_quantity}/{self.quantity})"