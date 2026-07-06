
# 委託單生命週期系統架構設計 (GSD Phase 2)

## 1. 核心組件設計

### 1.1 OrderManager (核心管理器)
- 統一管理所有委託單生命週期
- 提供標準化API給期貨、選擇權、股票系統
- 處理狀態轉換、事件分發、超時檢查

### 1.2 OrderLifecycle (生命週期狀態機)
- 擴展現有Order類別
- 完整狀態機：PENDING → SUBMITTED → PARTIAL_FILLED → FILLED
- 異常狀態：CANCELLED, REJECTED, EXPIRED
- 時間戳追蹤：created_at, submitted_at, filled_at, etc.

### 1.3 EventDispatcher (事件分發器)
- 統一處理Shioaji API回調
- 事件類型：ORDER_SUBMITTED, ORDER_FILLED, ORDER_CANCELLED, ORDER_REJECTED
- 支持多訂閱者模式

### 1.4 RiskValidator (風險驗證器)
- 統一風險檢查：資本限制、停損偏移、最大部位
- PAPER模式專用檢查
- 費用計算標準化

## 2. 系統整合設計

### 2.1 期貨系統整合
- 修改 strategies/futures/monitor.py
- 使用OrderManager提交委託單
- 整合安全停損機制

### 2.2 選擇權系統整合
- 修改 strategies/options/options_engine/engine/order_manager.py
- 統一事件處理
- 整合選擇權專用停損邏輯

### 2.3 股票系統整合
- 修改 strategies/stocks/monitor.py
- 使用盤中零股委託單標準化
- 整合手續費計算

## 3. 數據模型設計

### 3.1 Order 擴展
- 添加 parent_order_id (分批下單)
- 添加 execution_quality (執行品質指標)
- 添加 risk_metadata (風險元數據)

### 3.2 OrderFill 擴展
- 多筆成交記錄
- 平均成交價計算
- 費用明細追蹤

### 3.3 Position 同步
- 確保PaperTrader.position是單一真相來源
- 實時同步委託單狀態到部位
- 支持PAPER模式模擬

## 4. 測試策略設計

### 4.1 單元測試
- Order狀態機測試
- OrderManager API測試
- 事件處理測試

### 4.2 整合測試
- 期貨委託單生命週期測試
- 選擇權委託單生命週期測試
- 股票委託單生命週期測試

### 4.3 PAPER模式測試
- 資本限制測試
- 費用計算測試
- 停損偏移測試

## 5. 實施路線圖

### Phase 3: 實現核心
1. 實現OrderManager
2. 實現EventDispatcher
3. 實現RiskValidator

### Phase 4: 整合測試
1. 期貨系統整合
2. 選擇權系統整合
3. 股票系統整合

### Phase 5: 驗證優化
1. 完整測試套件
2. 性能優化
3. 文檔更新
