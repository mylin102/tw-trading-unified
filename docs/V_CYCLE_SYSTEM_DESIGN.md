# V-Cycle Phase 2: 系統設計文檔

## 1. 系統架構概覽

```
┌─────────────────────────────────────────────────────────────┐
│                    Trading System                           │
├─────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐        │
│  │   Futures   │  │   Options   │  │   Stocks    │        │
│  │   Monitor   │  │   Monitor   │  │   Monitor   │        │
│  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘        │
│         │                │                │               │
├─────────┼────────────────┼────────────────┼───────────────┤
│         ▼                ▼                ▼               │
│  ┌─────────────────────────────────────────────────────┐  │
│  │           Unified Order Lifecycle System            │  │
│  ├─────────────────────────────────────────────────────┤  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │  │
│  │  │ OrderManager│  │ RiskValidator│  │ EventSystem │ │  │
│  │  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘ │  │
│  │         │                │                │        │  │
│  └─────────┼────────────────┼────────────────┼────────┘  │
│            ▼                ▼                ▼           │
│  ┌─────────────────────────────────────────────────────┐  │
│  │              Broker Adapter Layer                   │  │
│  ├─────────────────────────────────────────────────────┤  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │  │
│  │  │ Shioaji 1.3 │  │ PaperSim    │  │ Live Broker │ │  │
│  │  │   API       │  │   Adapter   │  │   Adapter   │ │  │
│  │  └─────────────┘  └─────────────┘  └─────────────┘ │  │
│  └─────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

## 2. 核心組件設計

### 2.1 OrderManager (增強版)
```python
class EnhancedOrderManager:
    """增強版委託單管理器"""
    
    def __init__(self, mode="paper", broker_adapter=None):
        self.mode = mode
        self.broker_adapter = broker_adapter
        
        # 核心組件
        self.risk_validator = RiskValidator(mode)
        self.event_system = EventSystem()
        self.monitor = OrderMonitor()
        
        # 數據存儲
        self.active_orders = {}
        self.completed_orders = []
        self.order_history = OrderHistory()
        
        # 性能監控
        self.metrics = OrderMetrics()
        
    def submit_order(self, order):
        """提交委託單（完整流程）"""
        # 1. 風險驗證
        if not self.risk_validator.validate(order):
            return {"success": False, "error": "風險驗證失敗"}
        
        # 2. 創建委託單記錄
        self._create_order_record(order)
        
        # 3. 提交到經紀商
        result = self.broker_adapter.submit(order)
        
        # 4. 事件通知
        self.event_system.emit("order_submitted", order)
        
        # 5. 開始監控
        self.monitor.start_monitoring(order)
        
        return result
```

### 2.2 RiskValidator (風險驗證器)
```python
class RiskValidator:
    """統一風險驗證系統"""
    
    def __init__(self, mode="paper"):
        self.mode = mode
        self.config = self._load_config()
        
    def validate(self, order):
        """驗證委託單風險"""
        checks = [
            self._check_capital_limit(order),
            self._check_stop_loss_offset(order),
            self._check_position_limit(order),
            self._check_market_hours(order),
            self._calculate_fees(order)
        ]
        
        return all(checks)
    
    def _check_capital_limit(self, order):
        """檢查資本限制"""
        if self.mode == "paper":
            return order.estimated_cost <= 40000  # 40,000 TWD 限制
        return True
    
    def _check_stop_loss_offset(self, order):
        """檢查停損偏移"""
        if order.stop_loss:
            offset = abs(order.stop_loss - order.price)
            return offset >= 10  # ≥ 10 點
        return True
```

### 2.3 EventSystem (事件系統)
```python
class EventSystem:
    """完整的事件分發系統"""
    
    def __init__(self):
        self.subscribers = {}
        self.event_queue = EventQueue()
        self.event_store = EventStore()
        self.retry_manager = RetryManager()
        
    def emit(self, event_type, data):
        """發送事件"""
        # 1. 持久化事件
        self.event_store.save(event_type, data)
        
        # 2. 加入隊列
        self.event_queue.put(event_type, data)
        
        # 3. 分發給訂閱者
        self._dispatch(event_type, data)
        
    def subscribe(self, event_type, callback, retry_policy=None):
        """訂閱事件"""
        if event_type not in self.subscribers:
            self.subscribers[event_type] = []
        
        subscriber = {
            "callback": callback,
            "retry_policy": retry_policy or {"max_retries": 3, "delay": 1}
        }
        self.subscribers[event_type].append(subscriber)
```

### 2.4 OrderMonitor (訂單監控器)
```python
class OrderMonitor:
    """委託單監控和警報系統"""
    
    def __init__(self):
        self.monitored_orders = {}
        self.alerts = AlertSystem()
        self.timeout_checker = TimeoutChecker()
        
    def start_monitoring(self, order):
        """開始監控委託單"""
        self.monitored_orders[order.id] = {
            "order": order,
            "start_time": time.time(),
            "timeout": order.timeout or 300,  # 預設5分鐘
            "status": "monitoring"
        }
        
        # 啟動超時檢查
        self.timeout_checker.schedule_check(order.id, order.timeout)
        
    def check_timeout(self, order_id):
        """檢查委託單是否超時"""
        if order_id in self.monitored_orders:
            order_info = self.monitored_orders[order_id]
            elapsed = time.time() - order_info["start_time"]
            
            if elapsed > order_info["timeout"]:
                self.alerts.send("order_timeout", {
                    "order_id": order_id,
                    "elapsed": elapsed
                })
                return True
        return False
```

## 3. 數據流設計

### 3.1 委託單提交流程
```
1. 策略信號 → 2. OrderManager.create_order() → 3. RiskValidator.validate()
   ↓
4. EventSystem.emit("order_created") → 5. BrokerAdapter.submit()
   ↓
6. Shioaji API / PaperSimulator → 7. 成交回報
   ↓
8. EventSystem.emit("order_filled") → 9. PaperTrader.update_position()
   ↓
10. OrderMonitor.stop_monitoring() → 11. Metrics.record_execution()
```

### 3.2 事件處理流程
```
事件發生 → EventSystem.emit() → 事件持久化 → 加入隊列
   ↓
分發給訂閱者 → 執行回調 → 成功/失敗處理
   ↓
失敗重試 → 重試成功/最終失敗 → 警報通知
```

## 4. 數據庫設計

### 4.1 委託單表 (orders)
```sql
CREATE TABLE orders (
    id VARCHAR(32) PRIMARY KEY,
    symbol VARCHAR(10),
    side VARCHAR(4),
    order_type VARCHAR(20),
    quantity INTEGER,
    price DECIMAL(10,2),
    stop_price DECIMAL(10,2),
    status VARCHAR(20),
    filled_quantity INTEGER,
    avg_fill_price DECIMAL(10,2),
    commission DECIMAL(10,2),
    tax DECIMAL(10,2),
    total_fee DECIMAL(10,2),
    created_at TIMESTAMP,
    submitted_at TIMESTAMP,
    filled_at TIMESTAMP,
    cancelled_at TIMESTAMP,
    rejected_at TIMESTAMP,
    expired_at TIMESTAMP,
    exchange_order_id VARCHAR(50),
    strategy VARCHAR(50),
    account VARCHAR(50),
    parent_order_id VARCHAR(32)
);
```

### 4.2 事件表 (events)
```sql
CREATE TABLE events (
    id SERIAL PRIMARY KEY,
    event_type VARCHAR(50),
    order_id VARCHAR(32),
    data JSONB,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    processed BOOLEAN DEFAULT FALSE
);
```

### 4.3 審計日誌表 (audit_logs)
```sql
CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    log_type VARCHAR(50),
    order_id VARCHAR(32),
    message TEXT,
    details JSONB,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## 5. 接口設計

### 5.1 OrderManager API
```python
# 創建委託單
POST /api/orders
{
    "symbol": "TMF",
    "side": "BUY",
    "order_type": "MARKET",
    "quantity": 1,
    "price": 36500,
    "strategy": "counter_vwap"
}

# 查詢委託單狀態
GET /api/orders/{order_id}

# 取消委託單
DELETE /api/orders/{order_id}

# 查詢活躍委託單
GET /api/orders/active

# 查詢歷史委託單
GET /api/orders/history
```

### 5.2 監控 API
```python
# 系統狀態
GET /api/status

# 性能指標
GET /api/metrics

# 警報列表
GET /api/alerts

# 日誌查詢
GET /api/logs
```

## 6. 錯誤處理設計

### 6.1 錯誤分類
```python
class OrderError(Exception):
    """委託單錯誤基類"""
    pass

class RiskValidationError(OrderError):
    """風險驗證錯誤"""
    pass

class BrokerAPIError(OrderError):
    """經紀商API錯誤"""
    pass

class TimeoutError(OrderError):
    """超時錯誤"""
    pass

class InsufficientFundsError(RiskValidationError):
    """資金不足錯誤"""
    pass
```

### 6.2 錯誤處理策略
```python
def handle_order_error(error):
    """統一錯誤處理"""
    if isinstance(error, RiskValidationError):
        # 記錄審計日誌，不重試
        audit_log("risk_validation_failed", error)
        return {"success": False, "error": str(error)}
    
    elif isinstance(error, BrokerAPIError):
        # 可重試錯誤，實現指數退避
        if should_retry(error):
            schedule_retry(order, error)
        return {"success": False, "error": "broker_api_error"}
    
    elif isinstance(error, TimeoutError):
        # 超時錯誤，發送警報
        send_alert("order_timeout", order)
        return {"success": False, "error": "timeout"}
    
    else:
        # 未知錯誤，記錄並警報
        log_critical_error(error)
        send_alert("system_error", error)
        return {"success": False, "error": "system_error"}
```

## 7. 性能設計

### 7.1 快取策略
```python
class OrderCache:
    """委託單快取"""
    
    def __init__(self):
        self.order_cache = LRUCache(maxsize=1000)
        self.contract_cache = LRUCache(maxsize=100)
        self.market_data_cache = TTLCache(maxsize=100, ttl=60)  # 60秒過期
    
    def get_order(self, order_id):
        """從快取獲取委託單"""
        if order_id in self.order_cache:
            return self.order_cache[order_id]
        
        # 從數據庫加載
        order = db.get_order(order_id)
        if order:
            self.order_cache[order_id] = order
        return order
```

### 7.2 非同步處理
```python
async def process_order_async(order):
    """非同步處理委託單"""
    # 並發執行多個檢查
    tasks = [
        risk_check_async(order),
        market_check_async(order),
        position_check_async(order)
    ]
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # 合併結果
    if all(results):
        return await submit_order_async(order)
    else:
        return {"success": False, "errors": results}
```

## 8. 部署設計

### 8.1 容器化部署
```dockerfile
# Dockerfile
FROM python:3.9-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .
CMD ["python", "main.py"]
```

### 8.2 PM2 配置
```javascript
// ecosystem.config.js
module.exports = {
  apps: [{
    name: 'order-lifecycle-system',
    script: 'main.py',
    interpreter: 'python3',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '1G',
    env: {
      NODE_ENV: 'production'
    }
  }]
};
```

## 9. 測試策略

### 9.1 測試金字塔
```
        ┌─────────────────┐
        │   E2E Tests     │  (10%)
        └─────────────────┘
        ┌─────────────────┐
        │ Integration     │  (20%)
        │   Tests         │
        └─────────────────┘
        ┌─────────────────┐
        │   Unit Tests    │  (70%)
        └─────────────────┘
```

### 9.2 測試覆蓋率目標
- **單元測試**: 90%+ 代碼覆蓋率
- **整合測試**: 所有組件接口
- **E2E測試**: 完整交易流程
- **性能測試**: 響應時間和吞吐量

## 10. 監控和運維

### 10.1 監控指標
- **業務指標**: 委託單成功率、成交率、滑價
- **性能指標**: 響應時間、吞吐量、錯誤率
- **系統指標**: CPU、記憶體、磁盤、網絡

### 10.2 警報規則
- **錯誤率 > 5%**: 警告
- **響應時間 > 500ms**: 警告
- **系統重啟 > 3次/小時**: 嚴重
- **數據不一致**: 嚴重

### 10.3 日誌策略
- **訪問日誌**: 所有 API 請求
- **審計日誌**: 所有委託單操作
- **錯誤日誌**: 所有異常和錯誤
- **性能日誌**: 關鍵操作耗時