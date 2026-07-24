# MTS Multi-Exit Strategy — 最終定案架構規格與治理規範 (v3.0)

**建立日期:** 2026-07-24  
**來源文件:** `docs/mts_multi_exit_strategy_phased_tasks.md` & `.hermes/plans/2026-07-24_mts-multi-exit-strategy.md`  
**定案狀態:** FINAL APPROVED (設計成熟，準備啟動 Wave 0 基準驗證)  
**觀察期約束:** SEP Shadow Production Window (`2026-07-23` ~ `2026-08-06`)  
**正式解凍日:** `2026-08-07 起`  

---

## 1. 執行摘要 (Executive Summary)

本規格定義 MTS (Minimal Tradable System) 多重退場策略 (Multi-Exit Strategy) 的最終架構。
架構在設計層面已達成高度成熟與責任分明，採用 **無狀態純 Protocol (`ExitPolicy[StateT, ConfigT]`)**、**強型別 `SpreadContext` & `next_state`**、**`MultiLegExitState` 重啟崩潰一致性** 以及 **分層 Telemetry 水位追蹤**。
**警示：** 設計成熟不代表執行安全已被證明。系統將嚴格遵循 Characterization-First 原則，唯有在 Parity、Crash Recovery、Deterministic Replay 與 SEP Evidence Gate 全數通過後，才具備 Production-Grade 實盤資格。

---

## 2. 核心架構七大鐵律 (Seven Architectural Invariants)

1. **Protocol 依賴完全封閉 (`ExitPolicy[StateT, ConfigT]`)**  
   Policy 內部不得隱含持有未被 Provenance 捕捉的設定或狀態：
   $$\text{Policy}.\text{evaluate}(\text{context: SpreadContext}, \text{state: StateT}, \text{config: ConfigT}) \longrightarrow \text{ExitEvaluation}[StateT]$$

2. **強型別 State & Evaluation**  
   回傳強型別 `next_state: StateT` 或 Discriminated Union，禁止使用 `Dict[str, Any]` 作為核心 State Patch，避免拼字錯誤與跨策略欄位污染。

3. **Policy Parity vs Execution Parity 語義分離**  
   Policy 僅輸出策略面意圖 (`Action`, `Leg`, `Reason`, `Proposed transition`)；Broker 物件、帳號、order type (IOC/ROD) 及 Sweep 重試由執行層維護。

4. **特定領域下單治理 (MTS Execution Coordinator)**  
   平倉治理器位於 `strategies/futures/mts/execution/multi_leg_exit_coordinator.py`。包含明確的 `MultiLegExitState` 重啟恢復契約，Deadline 以 Event-Time 與持久化時間重建，不單純依賴 Monotonic Clock。

5. **Telemetry 耐久等級與資料集合格門禁 (`DATASET_ELIGIBILITY_FAILED`)**  
   寫入日誌攜帶 `telemetry_sequence`, `dropped_since_last`, `queue_depth`, `writer_lag_ms`。若遺失率超過門檻，標記 `DATASET_ELIGIBILITY_FAILED`，防止特徵遺失產生數據偏誤 (Selection Bias)。

6. **完整 Candidate Event 生命週期**  
   記錄 `ENTRY_CANDIDATE`, `ENTRY_ACCEPTED`, `ENTRY_REJECTED`, `NO_TRADE`, `POLICY_EVALUATION`, `EXIT_DECISION_POINT`, `PERIODIC_TRAJECTORY_SAMPLE`。

7. **SEP 多層 Evidence Gates (超越 SHA-256)**  
   SHA-256 僅為容器雜湊；研究驗證必須過關 **Integrity, Schema, Replayability, Determinism, Leakage, Statistical Evidence, Promotion** 七大門禁。

---

## 3. 模組目錄結構 (Final Module Layout)

```text
strategies/futures/mts/
├── contracts.py                # 純輸入輸出 Enum (ExitFamily, ExitAction), Evaluation, Diagnostics
├── policy.py                   # ExitPolicy Protocol[StateT, ConfigT] (強型別純介面)
├── state.py                    # 各 Exit Family 之強型別 Immutable State (NormalReleaseState 等)
├── config.py                   # 強型別 Config Dataclasses (ResolvedSpreadPnlTrailConfig 等)
├── context_builder.py          # [ACL 核心] 將 Production/Broker 物件轉換為強型別 SpreadContext
├── economics.py                # ContractEconomics (使用 Decimal / integer TWD 精確運算)
├── registry.py                 # Policy 註冊表 (family + version -> Policy Class)
├── selector_contracts.py       # StrategySelectionResult, SelectionDecision (Wave 0 純合約)
├── policies/                   # 具體 Policy 實作
│   ├── normal_release.py       # NormalReleasePolicy
│   ├── spread_pnl_trail.py     # SpreadPnlTrailPolicy
│   └── reverse_harvest.py      # ReverseHarvestPolicy
└── execution/
    └── multi_leg_exit_coordinator.py  # [MTS 下單治理] 雙腿平倉 Async/Partial fill/Timeout/Sweep

core/research/mts/
├── episode.py                  # Episode 數據封裝
├── replay_engine.py            # 重播引擎
├── replay_eligibility.py       # 重播資格驗證
├── policy_tournament.py        # 策略競技場比對
├── outcome_attribution.py      # 歸因分析
├── evidence.py                 # Evidence Gate 驗證邏輯
└── dataset_builder.py          # 研究 Parquet 資料集建置器
```

---

## 4. 波次與細化步驟 (Wave Plan)

### 🔹 Wave 0 — Contracts, ACL & Baseline Characterization
* **目標**: 建立純合約、Context ACL、Provenance 閉包與 Characterization 基準測試。
* **驗收不變量**:
  - `contracts.py` 不包含 `NO_TRADE`；所有 dataclass `frozen=True`。
  - 無 Shioaji 型別、無 `datetime.now()` 牆上時鐘、無檔案系統 Side-effects、無 `Dict[str, Any]`。
  - 單元測試保證：`same input + state + config -> same output`。

### 🔹 Wave 1 — Characterization-First Normal Release Extraction
* **1A (Characterization)**: 捕捉黃金基準，覆蓋正常 Release、 Near/Far Leg、No trigger、Stale quote、Warmup、Duplicate tick、Session force exit、Restart recovery。
* **1B (Delegation Seam)**: 建立新 Dispatch seam 委派至舊函數。
* **1C (Pure Extraction)**: 移轉至 `NormalReleasePolicy`。
* **1D (Shadow Parity Soak)**: 在 Mini 執行雙軌評估但不移交下單權。
* **1E (Authority Switch)**: 經由驗證且 Freeze 解凍後，移交下單權威。
* **1F (Legacy Removal)**: 清理舊路徑。

### 🔹 Wave 2 — Spread PnL Trail, Coordinator & Telemetry Spool
* **內容**:
  - 實作 `SpreadPnlTrailPolicy` (`execution_enabled: false` 僅 shadow)。
  - 實作 `MTS MultiLegExitCoordinator` (含 `MultiLegExitState` 重啟恢復與 Deadline 控制)。
  - 建立 Telemetry Spool，紀錄全方位 Candidate Events。

### 🔹 Wave 3 — SEP Unified Replay Engine
* **內容**:
  - 在 `core/research/mts/` 建立 Replay 引擎。
  - 嚴格標記證據來源：`OBSERVED` vs `COUNTERFACTUAL_REPLAY`。
  - 通過 7 大 Evidence Gates 生成 Manifest。

### 🔹 Wave 4 — Reverse Harvest Policy Evaluation
* **內容**:
  - `ReverseHarvestPolicy` Replay-only 反事實評估、參數敏感度分析與風險包絡線驗證（非機器學習訓練）。

### 🔹 Wave 5 — Data-Driven Selector Research (5A~5E)
* **順序**:
  - `5A` 描述性統計 ➔ `5B` Rule-based 基線 ➔ `5C` 時間跨度 Out-of-sample 驗證 ➔ `5D` 僅在顯著擊敗基線時引入 Supervised Model (5E)。

---

## 5. 判定與下一步

> **架構定案宣言：** 此方案已具備 Production-Grade 實作的完整架構條件。系統已準備好正式啟動 **Wave 0**。Wave 0 的核心目標不是改動策略，而是證明：**現有行為能被 100% 精準地描述、封閉、重播與比較。**
