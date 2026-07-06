# 委託單生命週期技術規格 (GSD Phase 2)

## 1. OrderManager 類別設計

```python
class OrderManager:
    """統一委託單管理器"""
    
    def __init__(self, api=None, paper_mode=True, capital_limit=40000):
        self.api = api  # Shioaji API實例
        self.paper_mode = paper_mode
        self.capital_limit = capital_limit
        
        # 委託單存儲
        self.orders = {}  # order_id -> Order
        self.pending_orders = {}  # exchange_order_id -> Order
        self.filled_orders = {}  # 已成交訂單
        self.cancelled_orders = {}  # 已取消訂單
        
        # 事件處理
        self.event_dispatcher = EventDispatcher()
        self.risk_validator = RiskValidator(paper_mode, capital_limit)
        
        # 訂閱Shioaji事件
        if api:
            self._setup_shioaji_callbacks()
    
    def submit_order(self, order: Order) -> dict:
        """提交委託單"""
        # 1. 風險驗證
        if not self.risk_validator.validate_order(order):
            order.reject("風險驗證失敗")
            return {"success": False, "error": "風險驗證失敗"}
        
        # 2. 創建Shioaji訂單
        shioaji_order = self._create_shioaji_order(order)
        
        # 3. 提交到API
        try:
            trade = self.api.place_order(order.contract, shioaji_order)
            order.submit(trade.id)  # 更新狀態為SUBMITTED
            self.pending_orders[trade.id] = order
            
            # 4. 觸發事件
            self.event_dispatcher.dispatch(
                "ORDER_SUBMITTED", 
                {"order": order, "trade": trade}
            )
            
            return {"success": True, "trade": trade, "order": order}
            
        except Exception as e:
            order.reject(f"API提交失敗: {str(e)}")
            return {"success": False, "error": str(e)}
    
    def cancel_order(self, order_id: str, reason: str = "") -> bool:
        """取消委託單"""
        order = self.orders.get(order_id)
        if not order or not order.is_active():
            return False
        
        try:
            if order.exchange_order_id:
                trade = self._get_trade_by_id(order.exchange_order_id)
                if trade:
                    self.api.cancel_order(trade)
            
            order.cancel(reason)
            self.event_dispatcher.dispatch("ORDER_CANCELLED", {"order": order})
            return True
            
        except Exception as e:
            return False
    
    def check_order_status(self, order_id: str) -> dict:
        """檢查委託單狀態"""
        order = self.orders.get(order_id)
        if not order:
            return {"error": "訂單不存在"}
        
        if order.exchange_order_id:
            trade = self._get_trade_by_id(order.exchange_order_id)
            if trade:
                self.api.update_status(trade=trade)
                status = self._parse_shioaji_status(trade)
                
                # 更新本地狀態
                if status.get("filled_qty", 0) > 0:
                    order.fill(
                        fill_price=status["avg_price"],
                        fill_quantity=status["filled_qty"],
                        commission=status.get("commission", 0),
                        tax=status.get("tax", 0)
                    )
        
        return order.to_dict()
    
    def get_active_orders(self) -> list:
        """獲取活躍委託單"""
        return [o for o in self.orders.values() if o.is_active()]
    
    def get_order_summary(self) -> dict:
        """獲取委託單摘要"""
        return {
            "total_orders": len(self.orders),
            "active_orders": len(self.get_active_orders()),
            "filled_orders": len(self.filled_orders),
            "cancelled_orders": len(self.cancelled_orders),
            "total_commission": sum(o.commission for o in self.orders.values()),
            "total_tax": sum(o.tax for o in self.orders.values()),
        }
```

## 2. EventDispatcher 類別設計

```python
class EventDispatcher:
    """事件分發器"""
    
    def __init__(self):
        self.subscribers = defaultdict(list)  # event_type -> [callbacks]
    
    def subscribe(self, event_type: str, callback: callable):
        """訂閱事件"""
        self.subscribers[event_type].append(callback)
    
    def dispatch(self, event_type: str, data: dict):
        """分發事件"""
        for callback in self.subscribers.get(event_type, []):
            try:
                callback(event_type, data)
            except Exception as e:
                logger.error(f"事件回調失敗: {e}")
    
    def on_shioaji_order_event(self, stat, msg):
        """Shioaji訂單事件處理"""
        event_type = self._map_shioaji_event(stat)
        if event_type:
            self.dispatch(event_type, {
                "shioaji_stat": stat,
                "message": msg,
                "timestamp": datetime.now()
            })
    
    def _map_shioaji_event(self, stat) -> Optional[str]:
        """映射Shioaji事件到內部事件"""
        mapping = {
            sj.constant.OrderState.FuturesDeal: "ORDER_FILLED",
            sj.constant.OrderState.StockDeal: "ORDER_FILLED",
            sj.constant.OrderState.FuturesOrder: "ORDER_STATUS_CHANGED",
            sj.constant.OrderState.StockOrder: "ORDER_STATUS_CHANGED",
            # 其他事件映射...
        }
        return mapping.get(stat)
```

## 3. RiskValidator 類別設計

```python
class RiskValidator:
    """風險驗證器"""
    
    def __init__(self, paper_mode=True, capital_limit=40000):
        self.paper_mode = paper_mode
        self.capital_limit = capital_limit
        self.min_stop_loss_offset = 10  # 最小停損偏移點數
    
    def validate_order(self, order: Order) -> bool:
        """驗證委託單風險"""
        checks = [
            self._check_capital_limit(order),
            self._check_stop_loss_offset(order),
            self._check_max_position(order),
            self._check_market_hours(order),
            self._check_fee_calculation(order),
        ]
        return all(checks)
    
    def _check_capital_limit(self, order: Order) -> bool:
        """檢查資本限制"""
        if not self.paper_mode:
            return True
        
        # 計算訂單所需資本
        order_capital = order.quantity * order.price
        if order.side == OrderSide.SELL:
            order_capital = 0  # 賣出不需要資本
        
        # 檢查是否超過限制
        total_exposure = self._calculate_total_exposure()
        return (total_exposure + order_capital) <= self.capital_limit
    
    def _check_stop_loss_offset(self, order: Order) -> bool:
        """檢查停損偏移"""
        if order.order_type != OrderType.STOP:
            return True
        
        if not order.stop_price or not order.price:
            return True
        
        offset = abs(order.stop_price - order.price)
        return offset >= self.min_stop_loss_offset
    
    def _check_fee_calculation(self, order: Order) -> bool:
        """檢查費用計算"""
        # PAPER模式必須包含費用
        if self.paper_mode:
            return order.commission > 0 and order.tax >= 0
        return True
```

## 4. 整合點設計

### 4.1 期貨系統整合點
```python
# 在 strategies/futures/monitor.py 中
class FuturesMonitor:
    def __init__(self, ...):
        self.order_manager = OrderManager(api=self.api, paper_mode=True)
        
        # 訂閱事件
        self.order_manager.event_dispatcher.subscribe(
            "ORDER_FILLED", 
            self._on_order_filled
        )
    
    def _place_order(self, action, price, quantity):
        """使用OrderManager下單"""
        order = Order(
            symbol=self.contract.code,
            side=OrderSide.BUY if action == "Buy" else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=quantity,
            price=price,
            strategy="counter_vwap",
            contract=self.contract
        )
        
        result = self.order_manager.submit_order(order)
        return result
```

### 4.2 選擇權系統整合點
```python
# 在 strategies/options/options_engine/engine/order_manager.py 中
class OptionsOrderManager:
    def __init__(self, ...):
        self.unified_order_manager = OrderManager(api=broker.api)
        
        # 保留選擇權專用邏輯
        self.option_stops = {}
        self.trailing_stops = {}
    
    def submit_entry(self, ...):
        """使用統一OrderManager提交進場單"""
        order = self._create_option_order(...)
        return self.unified_order_manager.submit_order(order)
```

### 4.3 股票系統整合點
```python
# 在 strategies/stocks/monitor.py 中
class StockMonitor:
    def _execute_trade(self, ticker, action, price, qty, reason):
        """使用OrderManager執行交易"""
        order = Order(
            symbol=ticker,
            side=OrderSide.BUY if action == "BUY" else OrderSide.SELL,
            order_type=OrderType.LIMIT,
            quantity=qty,
            price=price,
            strategy="mean_reversion_enhanced",
            order_lot=sj.constant.StockOrderLot.IntradayOdd  # 盤中零股
        )
        
        result = self.order_manager.submit_order(order)
        if result["success"]:
            self._update_position(ticker, action, qty, price)
```

## 5. 測試策略

### 5.1 測試檔案結構
```
tests/
├── test_order_lifecycle/
│   ├── test_order_manager.py
│   ├── test_event_dispatcher.py
│   ├── test_risk_validator.py
│   └── test_integration.py
├── test_futures_order.py
├── test_options_order.py
└── test_stocks_order.py
```

### 5.2 關鍵測試案例
1. **狀態機測試**: 驗證所有狀態轉換
2. **PAPER模式測試**: 資本限制、費用計算
3. **事件處理測試**: 回調、超時處理
4. **整合測試**: 各系統與OrderManager整合
5. **錯誤處理測試**: API失敗、網路中斷

## 6. 實施檢查清單

### Phase 3 檢查點
- [ ] OrderManager 實現完成
- [ ] EventDispatcher 實現完成  
- [ ] RiskValidator 實現完成
- [ ] 單元測試通過

### Phase 4 檢查點
- [ ] 期貨系統整合完成
- [ ] 選擇權系統整合完成
- [ ] 股票系統整合完成
- [ ] 整合測試通過

### Phase 5 檢查點
- [ ] 完整測試套件通過
- [ ] 性能基準測試完成
- [ ] 文檔更新完成
- [ ] 回歸測試通過 (287/288+)