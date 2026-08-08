# A4(theta) Release Timing Engine — Code Map（research-only）

**Commit**: engine implementation（c1a5695c / 631edca2 / c0325ee3）
**Scope**: scripts/research/release_timing_a4/** + 共享
scripts/research/phase_transition_replay/stream.py
**邊界**: 不碰 strategy/config/monitor/dashboard/ecosystem; 不執行真實
historical artifact; 不 deploy/restart/push

## 實際資料來源（三核心契約）

### 1. Event stream（不可變、全域時鐘）
- **來源**: runtime 匯出檔經 exit_attribution `reconcile_fill` 對帳後的
  fill/event 記錄（scripts/research/exit_attribution/reconcile.py —
  既有研究元件, A4 不自行 ad hoc 載入）
- **stream.ordered_stream(events, "immutable-global")**:
  - 排序鍵 (exchange_ts, source_event_seq); 每 event 注入 replay_seq
  - sha256(ordered manifest) = stream hash — 四分支 (A0..A3) 共用同一
    events object → 同一 hash → 同一 derived bars（branches.derived_bars
    回報 input_id 證明輸入參照同一）
- **時鐘契約**: exchange_ts（交易所時間）為主鍵; recv_ts 保留為
  收訊延遲證據; replay_seq 為研究重播序

### 2. State clone（strictly before breach, 絕不觸及 actual-release）
- **來源**: controller/lifecycle/positions/guard/ATR/reference 欄位由
  對帳後的 event 序列重建（研究內 clone 為欄位佔位值 — 實際值於引擎
  對真實對帳資料 run 時填入; 目前無 real artifact 執行）
- **A4 真委派 canonical**: A4 `breach.clone_from_state` 是 schema
  adapter — 實際呼叫
  `phase_transition_replay.clone.clone_from_state(...,
  schema_fields=A4_CLONE_SCHEMA_FIELDS)`; deep-copy / canonical hash /
  stream-prefix 的 primitive **只存在 canonical 一處**（monkeypatch
  call proof 鎖定）
- **strictly-before 語意**: 只讀 `replay_seq < breach_replay_seq` —
  **breach event 本身**與 release/future events 永不讀取（prefix hash
  測試含 BREACH + RELEASE sentinel）
- **clone_point_before_breach(event_seq, missing_fields=..., ...)**:
  - 任何缺欄 → typed ("NOT_AVAILABLE", [精確缺欄]) — 永不靜默部分 clone
  - actual_branch_mutated 參數被契約忽略 — clone 看不到 release 分支
    產生的未來 state
- **schema**: positions / policy_peak / guard_warmup / guard_armed /
  atr / reference_prices / pending_candidates / pending_orders /
  quote_freshness / controller / lifecycle / cooldown /
  strategy_generation / config_version（14 欄 + event_seq + version）

### 3. Evidence tier（決策點分類）
- **來源**: 決策時點的 quote 快照（兩腿 bid/ask + 各自 age_s —
  research 輸入契約, 由對帳 event 攜帶）
- **evidence_tier(quotes, decision_ts, staleness_bounds)**:
  - 兩腿 fresh → EXECUTABLE_BBO
  - 單腿 fresh → BOUNDED_PROXY
  - 僅 mark/last → MARK_PROXY
  - 無 → NOT_AVAILABLE
  - **never_claim_executable_without_bbo**: 無歷史 BBO 永不宣稱
    executable（fail-closed）
- **params_from_config(config, event)**: 門檻依 event 從 deployed
  config 解析; config 缺失 → NOT_AVAILABLE（fail-closed）

## 分類（frozen precedence, 報告複用 replay classify 契約）
arm_matrix 依 interval dominance 計算:
1. evidence gate（evidence != ok → INDETERMINATE_DATA_QUALITY）
2. MANAGEMENT_BAD（保守）: L3-U0 > M_economic AND L3 >= U(F_N)-M
3. F_N vs F_R interval dominance → HARMFUL / BENEFICIAL
4. 重疊 → INCONCLUSIVE_NEUTRAL
- 四 absolute Y 精確值 + 六 named pairwise deltas（d01..d23）
- 0/-100/-200 為 nested sensitivity rows（全數報告, 無 winner）

## 狀態機（假說限定）
NORMAL →(breach)→ RELEASE_ARMED →(reversal/新決策點)→ R0/R1/R2/R3
- **safety escape = terminal**（TerminalDecision）: combined_loss_floor /
  max_adverse_excursion / max_wait / quote_data_quality /
  lifecycle_pending — 觸發後 R3 不得 continue（decide 拋出）
- A4 只 ARM 風險, 本身不執行 release

## 測試
- tests/research/test_release_timing_a4.py — 24 passed（契約全轉綠）
- tests/research/test_phase_transition_replay.py — stream 契約轉綠 1 案
  （其餘 skeletal RED 維持 — replay engine 尚未實作）
