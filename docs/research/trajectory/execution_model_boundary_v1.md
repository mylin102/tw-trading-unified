# Execution and Fill Model Boundary v1 (執行與撮合模型邊界規範)

---

## 1. Model Levels and Decorators (撮合層級與裝飾器)
反事實執行模型透過可堆疊的裝飾器（Decorators）來逼近真實成交：

1. **`IdealTouchModel` (基準理想層)**：
   * 判定價格是否曾觸及委託限價，或市價單直接對齊 Tick 對價。成交口數受限於當時 Tick 報價的可用口數。
2. **`LatencyDecorator` (延遲影響層)**：
   * 虛擬委託送出至成交回報之間加入隨機或均值網路延遲（如 $20\text{ ms} \pm 5\text{ ms}$）。
3. **`SlippageDecorator` (滑價損耗層)**：
   * 根據委託口數與當時買賣五檔的口數深度，計算滑價造成的價格劣變。
4. **`FeeAndTaxDecorator` (交易成本層)**：
   * 精確扣減手續費與期交稅，確保 PnL 計算之真實性。

---

## 2. Realism Provenance Flags (真實性特徵標記)
重播報告必須根據啟用的 Decorators 組合，在 Provenance 欄位中寫入對應的真實性標誌，不得混淆：

| 啟用組合 (Decorators Enabled) | Realism Flag | 說明與適用研究範疇 |
| :--- | :--- | :--- |
| `IdealTouch` + `FeeAndTax` | **`IDEALIZED`** | **理想反事實**：可用於初步評估參數是否造成決策大方向變化。不代表最終實戰損益。 |
| `IdealTouch` + `Latency` + `Slippage` + `FeeAndTax` | **`SIMULATED_ROBUST`** | **健壯模擬**：考慮了延遲與滑價的壓力測試，可用於參數容錯區間評估。 |
| 僅對齊歷史成交 (未分叉前) | **`HISTORICAL_RECORDED`** | **歷史記錄**：代表實戰真實發生的盈虧。 |

---

## 3. Disallowed Claims (嚴格禁止之結論宣稱)
重播與分析系統在導出報告時，若 `performance_realism == "IDEALIZED"`，研究報告之結論中**嚴禁出現**以下說法：
* ❌「本參數變更可使策略收益提高 X TWD。」
* ❌「經優化後，最大回撤降低了 Y%。」
* ❌「本參數已達到生產環境上線標準。」

所有理想撮合下的績效輸出，必須加註顯著警告標語，以防研究人員產生過度樂觀之偏差。
