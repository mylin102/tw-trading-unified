# ADR-020: Historical Truth Boundary and Counterfactual Forking (歷史真實邊界與反事實分叉)

## Status
Proposed (Draft)

## Context (背景)
在動態軌跡重播中，最關鍵的因果控制在於**「路徑分歧點 (Divergence Point)」的識別與處理**。
當我們調整策略參數時：
1. 在模擬的最初階段，虛擬策略與歷史策略的決策完全一致（時間、方向、委託種類皆同）。
2. 在某一時刻，因為參數的改變，虛擬策略發出了一個與歷史不同的委託（如提早發出 Release，或者修改了委託價格），此時路徑發生**分歧 (Divergence)**。

一旦分歧發生，**後續所有的歷史因果關係即被打破**：
* 歷史上在此時點之後發生的 `BROKER_FILL` 成交回報、`POSITION_STATE` 與 `LIFECYCLE_TRANSITION` 不再代表真實情況，因為此時虛擬策略的持倉與委託已經與歷史不同。
* 如果重播引擎仍繼續注入歷史成交回報，將導致**嚴重的因果倒置與邏輯污染**。

因此，我們必須定義清晰的 **Historical Truth Boundary (歷史真實邊界)** 判定規則，以及在邊界越過後的 **Counterfactual Forking (反事實分叉) 機制**。

## Decision (決策)
我們決定在 Trajectory Replay 引擎中引入「歷史真實邊界與動態分叉過濾器」。當重播執行時，系統依據以下規則動態變更事件流的治理狀態：

```
                    Historical Truth Boundary
                                │
[Before Boundary]               │ [After Boundary (Forked)]
- All Historical Events Valid   │ - Invalidate Historical Endogenous Events
- Trace reproducing history     │ - Inject Counterfactual Simulated Events
                                ▼
───●──────────●──────────●──────┼───○──────────○──────────○────➔ Event Timeline
 MARKET_TICK  BROKER_ACK  BROKER_FILL  VIRTUAL_SUBMIT  VIRTUAL_FILL (Simulated)
```

### 1. 歷史真實邊界判定 (Divergence Detection Rules)
重播引擎會實時對比虛擬生成的 `VIRTUAL_ORDER_SUBMIT` 與歷史數據集中的委託。當滿足以下任一條件時，系統判定抵達**歷史真實邊界**，並觸發 **Counterfactual Forking**：

* **意圖不匹配 (Intent Mismatch)**：虛擬委託的方向（Side）、商品（Symbol）、腳數（Leg）、或意圖類別與對應時間點的歷史委託不一致。
* **時間漂移超限 (Timing Divergence)**：虛擬委託發出時間與歷史委託時間差值 $|\Delta t| > \text{Tolerance}$。
* **狀態不匹配 (FSM State Divergence)**：虛擬狀態轉移與歷史狀態轉移不吻合。
* **部位不對齊 (Position Divergence)**：虛擬持倉數量或方向與歷史記錄發生漂移。

觸發分叉的事件將被記錄為 `fork_event_id` 與 `fork_reason`。

### 2. 分叉後事件流過濾與生成規則 (Forking Execution Rules)
一旦越過真實邊界，系統自動將重播流程切換至 **Counterfactual Mode**，並套用以下過濾規則：

| 事件類別 (Event Category) | 處理規則 (Policy) | 說明與細節 |
| :--- | :--- | :--- |
| **外生市場事件** (`MARKET_TICK`, `SESSION_BOUNDARY`) | **保留 (Retain)** | 外生交易所行情與時間時段仍是唯一真實市場事實，必須繼續注入。 |
| **基礎建設事件** (`PROCESS_RESTART`, `BROKER_DISCONNECT`) | **保留並對齊 (Align)** | 基礎設施重啟與斷線事件仍代表當時物理環境限制，維持注入，但其導致的狀態對齊必須配合虛擬部位進行重算。 |
| **歷史內生事件** (`BROKER_ACK`, `BROKER_FILL`, `LIFECYCLE_TRANSITION`) | **作廢 (Invalidate)** | 歷史成交回報、委託確認與實戰狀態移轉一律丟棄，不再注入策略引擎。 |
| **虛擬生成事件** (`VIRTUAL_FILL`, `VIRTUAL_LIFECYCLE`) | **生成並注入 (Inject)** | 策略後續委託由模擬 `Execution Model`（ADR-021）結合外生 `MARKET_TICK` 進行撮合，生成對應的虛擬成交事件回報給虛擬策略。 |

### 3. 部分分叉與極端情境處理 (Corner Cases)
* **委託相同但時間不同**：即使虛擬策略發出與歷史相同的委託，但若時間偏移超過 500ms，代表策略在市場上的暴險時點已變，必須分叉，不允許沿用歷史成交。
* **數量改變 (Quantity Divergence)**：若參數修改導致委託口數（Quantity）改變，歷史成交價與量無法再被信任，必須分叉並由虛擬執行模型接管撮合。

## Consequences (後果)
1. **保證反事實正確性**：本分叉模型確保了在策略行為漂移後，重播系統能自動切斷歷史內生成交，改由虛擬撮合模型與真實 Tick 報價互動，徹底防範了「因果倒置」問題。
2. **開發複雜性移轉**：此決策意味著我們必須配備一個精準的 `Execution and Fill Model`（ADR-021）來接管分叉後的成交生成，否則分叉後的軌跡將卡死或不合常理。
