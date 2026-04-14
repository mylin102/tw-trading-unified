"""
委託單成交記錄類別
記錄單一成交的詳細資訊
"""

from datetime import datetime
from typing import Optional
import json


class OrderFill:
    """委託單成交記錄，記錄單一成交的詳細資訊"""
    
    def __init__(
        self,
        order_id: str,
        fill_price: float,
        fill_quantity: int,
        commission: float = 0.0,
        tax: float = 0.0,
        fill_id: Optional[str] = None,
        exchange_fill_id: Optional[str] = None,
        fill_time: Optional[datetime] = None,
    ):
        """
        初始化成交記錄
        
        Args:
            order_id: 對應的委託單ID
            fill_price: 成交價格
            fill_quantity: 成交數量
            commission: 手續費
            tax: 交易稅
            fill_id: 成交記錄ID (自動生成如果為None)
            exchange_fill_id: 交易所成交ID
            fill_time: 成交時間 (現在如果為None)
        """
        self.fill_id = fill_id or f"fill_{order_id}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
        self.order_id = order_id
        self.exchange_fill_id = exchange_fill_id
        self.fill_price = fill_price
        self.fill_quantity = fill_quantity
        self.commission = commission
        self.tax = tax
        self.total_fee = commission + tax
        self.fill_time = fill_time or datetime.now()
        
        # 計算成交金額
        self.fill_amount = fill_price * fill_quantity
        
    def to_dict(self) -> dict:
        """轉換為字典格式"""
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "exchange_fill_id": self.exchange_fill_id,
            "fill_price": self.fill_price,
            "fill_quantity": self.fill_quantity,
            "fill_amount": self.fill_amount,
            "commission": self.commission,
            "tax": self.tax,
            "total_fee": self.total_fee,
            "fill_time": self.fill_time.isoformat(),
        }
        
    def to_json(self) -> str:
        """轉換為JSON格式"""
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)
        
    @classmethod
    def from_dict(cls, data: dict) -> "OrderFill":
        """從字典創建OrderFill實例"""
        # 解析時間戳記
        fill_time_str = data.get("fill_time")
        fill_time = None
        if fill_time_str:
            fill_time = datetime.fromisoformat(fill_time_str.replace("Z", "+00:00"))
        
        # 創建實例
        fill = cls(
            order_id=data["order_id"],
            fill_price=data["fill_price"],
            fill_quantity=data["fill_quantity"],
            commission=data.get("commission", 0.0),
            tax=data.get("tax", 0.0),
            fill_id=data.get("fill_id"),
            exchange_fill_id=data.get("exchange_fill_id"),
            fill_time=fill_time,
        )
        
        return fill
        
    def __repr__(self) -> str:
        return f"OrderFill({self.fill_id}, order={self.order_id}, price={self.fill_price}, qty={self.fill_quantity})"