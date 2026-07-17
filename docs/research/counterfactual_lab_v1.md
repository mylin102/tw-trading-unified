# Counterfactual Lab v1.0.0 — Research Constitution (研究憲章)

---

## 1. Why (研究願景與初衷)
在 Taiwan Trading Unified 量化交易系統中，策略決策錯誤會導致直接且巨大的財務虧損。傳統回測平台常因「歷史模擬」與「生產引擎」之邏輯分裂，導致研究階段優化的參數在實戰中失效。
**Counterfactual Lab** 旨在建立一個具備**語意一致性**與**研究可審計性**的離線反事實實驗環境：
1. **Side-effect Isolation**：完全唯讀，透過 immutable configs 進行沙盒模擬，確保任何實驗都不會寫入生產資料庫、FSM 狀態檔或實戰日誌。
2. **Deterministic Governance**：提供確定性哈希與可重建索引，確保每一次研究結論都具備可追溯、可還原的證據鏈。

---

## 2. Architecture (基礎架構設計)
Counterfactual Lab v1.0.0 遵循嚴格的分層解耦設計：
* **`CounterfactualService`**：作為唯一的門面（Facade），不依賴任何 Streamlit 元件。
* **`ReplayConfig`**：採用 Immutable Dataclass。所有參數覆寫均透過 Config 傳遞，禁止直接 Mutate 策略物件內部狀態。
* **`Replay Dataset Contract`**：利用 Parquet 與 JSON Manifest 明確定義輸入格式，與實戰交易庫完全解耦。

---

## 3. Certified Scope (已認證研究範圍)
本版本（v1.0.0）僅認證並支援以下研究範疇：
* **基線決策點重播 (Point Replay Validation)**：比對歷史已記錄之 Action/Leg/Reason 與當前生產引擎 `evaluate_lifecycle_actions` 的輸出，以確保 100% 重現性。
* **一維決策點敏感度分析 (One-dimensional Parameter Sweep)**：對白名單參數（`release_stop_threshold`, `confirm_ms`, `confirm_ticks`）進行離散掃描，辨識個案決策點的漂移率（Drift Rate）與穩定邊界。

---

## 4. Governance (實驗治理原則)
* **規格化 Experiment Hash**：以 UTF-8、字典排序、緊湊 JSON 對 Baseline ID、Dataset Hash、Git Commit 與掃描參數進行 Canonicalization，產生定址雜湊。若代碼處於 Dirty 狀態，會強制計算 `dirty_diff_hash` 納入識別。
* **Experiment 與 Run 分離**：一個 Experiment (研究設計) 可對應多個 Runs (執行實例)。Runs 下記錄各自的 `result_hash` 以校驗輸出完整性。
* **原子寫入保護**：`registry.json` 索引之更新必須透過 `fcntl` 排他鎖、暫存檔 fsync 與原子 rename 進行，確保多線程存取安全。

---

## 5. Limitations & Known Non-goals (研究限制與非目標)
* **無軌跡模擬 (No Trajectory Replay)**：不支援隨時間演進的狀態轉移 Chronology 重建。
* **無反事實成交 (No Alternate Fills)**：不評估當參數改變時，歷史成交價或成交量是否會隨之改變。
* **無反事實績效 (No Counterfactual PnL)**：本階段無法、亦禁止用於推導模擬交易盈虧、最大回撤、或進行自動化生產參數優化。本系統非回測效能評估工具。

---

## 6. Research Roadmap (未來研究路線圖)
為安全推進反事實研究，後續 Phase 4 規劃將嚴格遵循契約先行：

```
Phase 3 (Point Replay) ➔ Phase 4A (Evidence Contract) ➔ Phase 4B (Reproduction Cert) ➔ Phase 4C (Path Divergence) ➔ Phase 4D (Execution Model)
```

1. **Phase 4A: Trajectory Evidence Model & Event Taxonomy**
   * 定義 `Event Taxonomy`（區分 Observed, Derived 與 Counterfactual 類型事件）。
   * 確立 `Deterministic Total Ordering Key` 排序規則，防範同 timestamp 順序漂移。
2. **Phase 4B: Baseline Trajectory Certification**
   * 驗證在原始參數下重播完整軌跡時，Lifecycle transition 與 Order intent 的一致性。
3. **Phase 4C: Historical Truth Boundary & Counterfactual Forking**
   * 精確定義因果分叉點。在路徑分岔後，宣告歷史 endogenous events 屬性失效，正式開啟 counterfactual simulator。
4. **Phase 4D: Execution and Fill Model**
   * 以 Swappable plugins 的方式加入 Latency, Slippage, Fees, Taxes 等執行層真實性裝飾器。
