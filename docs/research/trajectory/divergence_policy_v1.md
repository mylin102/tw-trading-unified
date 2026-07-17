# Divergence Policy v1 (路徑分歧與分叉政策)

---

## 1. Divergence Thresholds (分歧判定閾值)
當軌跡重播運行時，策略的任何偏離都會觸發判定。以下為硬性判定閾值：

| 評估維度 (Dimension) | 容許閾值 (Tolerance) | 超出後處理方式 (Action) |
| :--- | :--- | :--- |
| **委託送出時間偏移** | $\pm 500\text{ ms}$ | 超出後判定為 `TIME_DRIFT_DIVIDED`，觸發 Forking。 |
| **狀態機移轉對齊** | 狀態名必須 100% 相同 | 不符則判定為 `STATE_MISMATCH`，觸發 Forking。 |
| **委託價格偏移** | $\pm 0\text{ pt}$ (點數) | 不符則判定為 `PRICE_DIVERGENCE`，觸發 Forking。 |
| **部位數量不對齊** | $\pm 0\text{ unit}$ (口數) | 不符則判定為 `POSITION_DIVERGENCE`，觸發 Forking。 |

---

## 2. Invalidation Mechanics (歷史事件作廢機制)
當 Counterfactual Forking 觸發時，Replay Engine 必須執行以下記憶體清理與事件鏈重構動作：
1. **停止追蹤歷史回報**：停止讀取或注入後續的 `BROKER_ACK` 與 `BROKER_FILL` 事件。
2. **清除歷史未來持倉**：將任何預期讀取的歷史 `POSITION_STATE` 快照從緩衝區清除，改為透過虛擬持倉 `VirtualPosition` 實時推導。
3. **啟動模擬撮合器**：啟用 `ExecutionModel`（ADR-021）接管所有虛擬委託的生命週期。

---

## 3. Handling Partial Divergence (部分分歧與邊界案例)
在特殊情況下，策略偏離可能是局部的，例如：
* **委託相同但口數減少 (Quantity Reduced)**：
  * 例如歷史買進 2 口，虛擬策略僅買進 1 口。
  * 處理方式：視為**完全分歧**。雖然方向一致，但部位大小會影響隨後的 Release 判斷與出場時點。必須在該時點啟動 Forking，廢棄歷史第 2 口的成交回報，由虛擬模型模擬 1 口的交易路徑。
* **委託意圖相同但價格更優 (Favorable Price)**：
  * 虛擬策略以更低的價格掛限價單。
  * 處理方式：觸發 Forking。即使此委託看似能更快成交，但其撮合邏輯必須經過 `ExecutionModel` 驗證，不允許直接套用歷史成交價格。
