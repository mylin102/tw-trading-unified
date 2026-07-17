# ADR-018: Deterministic Event Ordering (確定性事件全序排序)

## Status
Proposed (Draft)

## Context (背景)
在進行**動態軌跡重播 (Trajectory Replay)** 時，研究者面臨最大的技術挑戰是**時間軸上的事件並行性 (Event Concurrency)**。在微秒或毫秒精度下，多個不同的事件（如市場 Tick、策略定時器超時、券商委託確認、成交回報）可能會共享完全相同的時間戳（Timestamp）。

如果 Replay 引擎隨機排序這些並行事件，將會導致：
1. **重播非確定性**：每次執行重播可能得到不同的 FSM 狀態或成交判定。
2. **因果倒置（Causality Inversion）**：例如在邏輯上應先收到券商 `BROKER_ACK` 才能處理 `BROKER_FILL`，若排序出錯，策略引擎會因為「先成交再確認委託」而拋出異常或轉移到錯誤狀態。

因此，我們必須定義一個絕對的、與物理時鐘或系統排程無關的**全序排序鍵值 (Deterministic Total Ordering Key)** 與**同時間事件優先級真值表 (Event Priority Truth Table)**。

## Decision (決策)
我們決定在 Trajectory Dataset 載入與重播時，強制以唯一的六元組 Ordering Key 進行排序：

### 1. 全序排序鍵值 (Deterministic Total Ordering Key)
排序比較順序由左至右依次判定：
```python
ordering_key = (
    event_time_ns,         # 1. 交易所/物理髮生時間戳 (納秒)
    source_priority,       # 2. 來源層級優先級 (整數，越小越優先)
    event_type_priority,   # 3. 事件類別優先級 (整數，越小越優先)
    source_sequence,       # 4. 來源端單調遞增序號
    receive_time_ns,       # 5. 系統接收並記錄時間戳 (納秒)
    event_id,              # 6. 唯一事件 ID (字串比對，作為最後 Stable Tie-Breaker)
)
```

### 2. 來源層級優先級 (Source Priority Map)
| 來源 (Source) | 優先級 (Priority Value) | 治理目的 |
| :--- | :--- | :--- |
| `exchange` | `10` | 市場外生事實最優先處理。 |
| `shioaji_broker` | `20` | 券商回報次之。 |
| `strategy_router` | `30` | 策略決策最晚處理，確保先消化市場與券商狀態。 |
| `system_monitor` | `40` | 基礎建設監控事件。 |

### 3. 事件優先級真值表 (Event Type Priority Truth Table)
當 `event_time_ns` 與 `source_priority` 相同時，依事件類型決定處理順序：

| 順序 (Order) | 事件類型 (Event Type) | 優先級 (Val) | 排序語意說明 (Semantic Context) |
| :---: | :--- | :---: | :--- |
| **1** | `SESSION_BOUNDARY` | `1` | 時段轉換與開收盤狀態必須最先更新。 |
| **2** | `MARKET_TICK` | `2` | 新的市場行情必須先被載入。 |
| **3** | `BROKER_DISCONNECT` | `3` | 斷線事件優先於任何業務回報。 |
| **4** | `BROKER_ACK` | `4` | 先處理委託送出成功確認。 |
| **5** | `BROKER_FILL` | `5` | 後處理成交，確保委託已存在。 |
| **6** | `LIFECYCLE_TRANSITION` | `6` | 策略狀態機移轉，在消化完所有市場與券商回報後執行。 |
| **7** | `PROCESS_RESTART` | `7` | 重啟等系統事件。 |

### 4. 異常與缺失策略 (Missing & Exception Policies)
* **`source_sequence` 缺失**：若某些歷史事件不包含遞增序號，預設填入 `0`，依賴 `receive_time_ns` 與 `event_id` 作為 Stable Tie-Breaker，確保排序在重複執行時依然不變。
* **Late-Arriving Events (遲到事件)**：若實戰日誌中某個 endogenous 回報在物理時間上比後續的 market tick 晚記錄，但其 `event_time` 較早，排序鍵值將強制將其拉回正確的邏輯時間順序。

## Consequences (後果)
1. **還原真實因果**：通過此全序鍵值，策略 FSM 在重播時將永遠先看到行情 tick ➔ 再看到委託確認 ➔ 最後看到成交回報，排除一切因並行性造成的幽靈狀態與 race conditions。
2. **跨平台一致性**：無論是在 macOS M4 效能核心或背景執行核心，亦或是 Linux CI 伺服器，事件隊列的排序順序均完全一致，為 Dynamic Replay 的 determinism 提供底層保障。
