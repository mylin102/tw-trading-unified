# Software Design Document (SDD) — tw-trading-unified

## 1. 今晚 Bug 根因分析

| Bug | 根因 | 分類 |
|-----|------|------|
| 期貨 BE offset 2 pts 不夠 cover 手續費 | 硬編碼常數，沒有與成本模型連動 | 設計缺陷 |
| 選擇權重複進場 5 次 10 口 | `position = paper_lots` 覆蓋而非檢查，重啟後 recovery 失敗 | 狀態管理 |
| PnL 沒乘口數 | `log_trade` 沒用 position qty | 計算錯誤 |
| 停損用停損價而非市場價出場 | `_check_stop_loss` 傳 stop_loss level 而非 market price | 邏輯錯誤 |
| CSV PnL 沒扣手續費 | `_execute_trade` 自己算 PnL，沒用 PaperTrader 的結果 | 重複邏輯 |
| `datetime.timedelta` crash | `from datetime import datetime` 覆蓋 module | 命名衝突 |
| EXIT 被重複觸發 5 次 | `manage_open_position` 每次 poll 都觸發，position 歸零後 log 還在算 | 狀態競爭 |
| 期貨重複下單 50+ 筆 | `save_trade` 在 `execute_signal` 之前 + JSON 全量覆寫 CSV | 執行順序 |
| 進場價 97.7 不合理 | 合約切換時 bid/ask 歸零，fallback 到錯誤報價 | 數據驗證 |

## 2. 架構問題（共通根因）

### 2.1 狀態管理無單一真相源 (Single Source of Truth)
- `position` 在 monitor、trader、ledger 三處各自維護
- 重啟後 recovery 從 ledger 讀，但 ledger 本身有重複紀錄
- **改善**：所有狀態以 `PaperTrader.position` 為唯一真相源

### 2.2 副作用先於驗證 (Side Effects Before Validation)
- `save_trade` 在 `execute_signal` 之前 → 寫了垃圾紀錄
- `log_trade` 在 `position = 0` 之前 → 重複計算 PnL
- **改善**：先驗證 → 再執行 → 最後記錄

### 2.3 缺乏防禦性程式設計
- 沒有 entry price 合理性檢查（97.7 的 ATM Call）
- 沒有 position guard 在 exit 路徑
- 沒有 paper 模式的資金限制
- **改善**：每個公開方法的第一行都做 precondition check

### 2.4 命名空間污染
- `from datetime import datetime` 覆蓋了 `datetime` module
- **改善**：統一用 `import datetime` 或 `from datetime import datetime as dt`

## 3. 模組職責定義

```
┌─────────────────────────────────────────────────────┐
│                    main.py                          │
│  職責：啟動、訂閱、分發、健康檢查                      │
│  不做：交易邏輯、狀態管理                              │
├─────────────────────────────────────────────────────┤
│          FuturesMonitor                             │
│  職責：指標計算、策略信號產生、呼叫 Executor           │
│  不做：直接操作 position、直接寫 CSV                   │
├─────────────────────────────────────────────────────┤
│          OptionsMonitor                             │
│  職責：Greeks 計算、信號產生、呼叫 Executor            │
│  不做：直接操作 position、直接寫 CSV                   │
├─────────────────────────────────────────────────────┤
│          TradeExecutor (新增)                        │
│  職責：驗證 → 執行 → 記錄（唯一寫入點）                │
│  保證：execute 成功才寫紀錄，position 是唯一真相源      │
├─────────────────────────────────────────────────────┤
│          DataStorage                                │
│  職責：持久化（CSV/JSON），不做業務邏輯                 │
└─────────────────────────────────────────────────────┘
```

## 4. 關鍵介面契約

### 4.1 TradeExecutor.execute()
```python
def execute(self, signal: str, price: float, lots: int, **kwargs) -> Optional[TradeResult]:
    """
    Preconditions:
      - signal in ("BUY", "SELL", "EXIT", "PARTIAL_EXIT")
      - price > 0
      - lots > 0
    
    Postconditions:
      - 成功：position 更新、紀錄寫入、return TradeResult
      - 失敗：position 不變、不寫紀錄、return None
    
    Invariants:
      - abs(position) <= max_positions at all times
      - 不會在同一根 bar 重複進場
      - entry price 必須通過合理性檢查
    """
```

### 4.2 進場 Precondition Checklist
```python
def _pre_entry_check(self, price, lots) -> bool:
    assert self.position == 0, "Already in position"
    assert price > 0, "Invalid price"
    assert self._is_price_reasonable(price), "Price out of range"
    assert self._margin_sufficient(price, lots), "Insufficient margin"
    assert self._not_same_bar(), "Already traded this bar"
    return True
```

### 4.3 出場 Precondition Checklist
```python
def _pre_exit_check(self) -> bool:
    assert self.position != 0, "No position to exit"
    assert self.entry_price > 0, "Invalid entry price"
    return True
```

## 5. 數據流（修正後）

```
Strategy Signal
    │
    ▼
TradeExecutor.execute()
    ├─ 1. Precondition check (position, margin, price)
    ├─ 2. PaperTrader.execute_signal() → position 更新
    ├─ 3. 成功？
    │   ├─ Yes → save_trade() + log_trade()
    │   └─ No  → return None（不寫任何東西）
    └─ 4. return TradeResult
```

## 6. 配置管理

### 6.1 Config 熱載入
```yaml
# 每次 _strategy_tick 重新讀取 active_strategy
# 不需重啟即可切換策略
strategy:
  active_strategy: squeeze_breakout  # dashboard 可即時修改
```

### 6.2 Config 驗證
```python
def validate_config(cfg):
    assert cfg["trade_mgmt"]["max_positions"] >= 0
    assert cfg["risk_mgmt"]["atr_multiplier"] > 0
    assert cfg["strategy"]["active_strategy"] in STRATEGIES
    assert cfg["risk_mgmt"]["stop_loss_pts"] > tick_cost  # 必須 > 手續費
```
