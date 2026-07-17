# Trajectory Replay Constitution (軌跡重播憲章)

---

## 1. Purpose (研究目的)
在決策點（Point-in-time）敏感度分析之後，**Trajectory Replay** 代表從「靜態決策分析」走向「動態路徑演進」的研究基石。其核心目的在於：
1. 重構完整的交易事件序列（Event Chronology），包含 Tick 行情、委託、成交回報與生命週期轉移。
2. 在完全確定的全序事件流下，認證策略引擎在連續時間軸上的行爲與持倉演變。
3. 為反事實持倉分歧與執行模擬（Performance Counterfactual）提供高信賴度的起點。

---

## 2. Invariant Rules (研究不變量原則)
為防止在軌跡模擬中引入人為偏差或偏離現實，本憲章確立以下三條核心不變量原則：

### 原則一：時間與因果單向性 (Single-direction Causality)
* 市場事件（Exogenous Events，如 Tick 行情與交易時段）是絕對客觀的事實，在反事實模擬中維持不變。
* 策略內生事件（Endogenous Events，如委託與 Lifecycle State）是策略決策產生的結果。
* 嚴禁「因果污染」（Causality Contamination）：在反事實分支中，歷史 Endogenous Events 對模擬器為不可見，所有內生狀態必須由模擬的 FSM 引擎與執行模型實時生成。

### 原則二：完全確定性全序排序 (Deterministic Total Ordering)
* Replay 引擎的執行結果必須與實體時間或作業系統排程解耦。
* 給定相同的 Dataset 與相同事件全序鍵值（Deterministic Total Ordering Key），不論何時、何地、由何人執行，軌跡重播結果必須 100% 重現。

### 原則三：先認證再反事實 (Reproduction Certification First)
* 在進行任何參數擾動（Counterfactual Run）前，必須首先在**原始基準參數**下運行軌跡重播。
* 必須通過嚴格的 `Baseline Trajectory Reproduction Certification` 檢驗（對持倉、狀態機序列、委託意圖等維度進行比對並在誤差容忍範圍內通過），方可證明 Replay Engine 具備充足的還原精度。

---

## 3. Boundary & Non-goals (研究邊界與非目標)
本憲章明確定義 Phase 4 的技術限制與不作承諾（Non-goals）：
* **非高頻實時模擬器**：本平台不模擬毫秒級的微觀網路抖動或排隊優先權，所有時間排序均基於已錄製的實戰事件順序。
* **分步式 PnL 認證**：在執行模型（Execution & Fill Model）未通過單獨的 `Execution Certification` 前，任何軌跡重播輸出的模擬 PnL 僅能標記為 `IDEALIZED_PNL`，不得作為生產最佳化決策的直接依據。
