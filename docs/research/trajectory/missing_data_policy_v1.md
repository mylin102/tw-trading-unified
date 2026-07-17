# Missing Data, Duplicates, and Stale Quotes Policy v1 (缺失、重複與過期資料處理政策)

---

## 1. Missing Events Policy (缺失事件政策)
在數據集編譯與軌跡重播中，可能會因為連線中斷、API 漏失或寫入失敗導致部分事件遺失：
* **關鍵外生市場事件缺失 (Missing Market Tick)**：
  * 若缺失時間段小於 `10 秒` 且不包含價格跳空，採用前值填充（Forward Fill）政策，使用最近的有效行情。
  * 若缺失時間段超過 `10 秒` 或跨越交易時段，直接標記該 Trajectory Case 為 `DATA_GAP_INVALID`，拒絕重播，防堵模擬偏差。
* **關鍵內生成交事件缺失 (Missing Endogenous Fill)**：
  * 若實戰數據集中包含發出委託但沒有成交回報（可能由於人工干預或記錄丟失），不進行隨意猜測填補。
  * 基線重播認證（ADR-019）會直接判定為 `FAILED` 並指出為 `Missing Historical Fill`，由研究者人工審核數據完整性。

---

## 2. Duplicate Events Policy (重複事件判定政策)
重複事件通常源於網路重試或多執行緒重複寫入：
* **判定準則**：若兩個事件的 `event_time_ns`、`event_type`、`source` 與 `payload` (內容經 canonical 雜湊比對) 完全一致，則判定為重複事件。
* **處理規則**：系統將自動丟棄（Discard）後收到的重複事件，僅保留並處理第一個進入排序隊列的事件，並在日誌中記錄 `DUPLICATE_EVENT_DISCARDED`。

---

## 3. Stale Quotes Policy (過期行情政策)
當系統斷線、報價 API 延遲或策略引擎運作緩慢時，所讀取的行情可能已經過期：
* **判定準則**：若 `receive_time_ns` 與 `event_time_ns` 的差值 $\Delta t > \text{Stale Threshold}$ (預設為 5 秒)，該 Tick 被視為過期報價 (Stale Quote)。
* **處理規則**：
  * 虛擬策略引擎將被拒絕使用此過期行情進行交易決策。
  * 模擬撮合器將標記該區間為 `QUOTE_STALE_INTERVAL`，直到收到最新的健康 Tick。
