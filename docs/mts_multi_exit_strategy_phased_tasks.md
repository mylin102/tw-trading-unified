# MTS Multi-Exit Strategy Program
## 階段性任務與 Agent 執行規格

**Status:** Proposed  
**Scope:** MTS Calendar Spread  
**Products:** TMF / MTX  
**Primary Goal:** 建立三種互斥的 MTS Exit Family，透過可重播、可比較、可治理的研究流程，逐步演進到 Dashboard 控制與 regime-based 自動選型。  
**Safety Principle:** 所有策略切換、參數變更與自動選型均須 fail-closed；持倉中的 Exit Family 必須 immutable。

---

# 1. Program Objective

現有 MTS 主要採用：

```text
虧損腿 RELEASE
→ 剩餘獲利腿 SINGLE_LEG
→ TRAIL
→ FLAT
```

本計畫將 MTS 明確拆分成三種互斥 Exit Family：

```text
1. NORMAL_RELEASE
2. REVERSE_HARVEST
3. SPREAD_PNL_TRAIL
```

並保留第四種正式決策結果：

```text
4. NO_TRADE
```

最終目標：

```text
Market / Spread / Leg Features
    ↓
Regime Assessment
    ↓
Strategy Eligibility
    ↓
Entry Decision
    ↓
Risk Gate
    ↓
Frozen Exit Family
    ↓
Deterministic Execution
```

---

# 2. Strategy Definitions

## 2.1 NORMAL_RELEASE

### 核心邏輯

```text
Cut loser, let winner run.
```

流程：

```text
ENTRY
→ SPREAD_ACTIVE
→ 虧損腿觸發 RELEASE
→ 平掉虧損腿
→ SINGLE_LEG
→ 獲利腿 TRAIL
→ FLAT
```

### 適用假說

- Spread divergence 持續。
- 某一腿形成方向性趨勢。
- Mean reversion 暫時失效。
- Winner continuation 具有正期望值。

### 主要風險

- 獲利腿發生大幅回吐。
- MFE 最終退化為低收益或淨損。
- PED 過大。

---

## 2.2 REVERSE_HARVEST

### 核心邏輯

```text
Harvest winner, allow loser a bounded recovery window.
```

流程：

```text
ENTRY
→ SPREAD_ACTIVE
→ 追蹤獲利腿峰值
→ 獲利腿自峰值回落
→ 平掉獲利腿
→ LOSER_RECOVERY
→ Recovery Trail / Hard Stop / Timeout
→ FLAT
```

### 適用假說

- Spread 仍具有均值回歸性。
- Winner momentum 衰退。
- Loser leg 有回復機率。
- 尚有足夠 session 時間。

### 主要風險

```text
Spread exposure
→ Naked directional exposure
```

因此必須具備：

- Loser absolute hard stop
- Trade-level locked-profit floor
- Recovery timeout
- Session forced exit
- Broker / quote health fail-closed

---

## 2.3 SPREAD_PNL_TRAIL

### 核心邏輯

```text
Trade the spread as a spread.
```

將雙腿合併 PnL 視為單一 position：

```text
combined_pnl =
    near_upl
    + far_upl
    + realized_leg_pnl
    - fees
    - estimated_exit_cost
```

流程：

```text
ENTRY
→ SPREAD_ACTIVE
→ Combined PnL 達 Arming Threshold
→ 追蹤 Combined PnL Peak
→ Giveback 超過門檻
→ 雙腿同步退出
→ FLAT
```

### 適用假說

- Alpha 主要來自 spread，而非單腿方向。
- 希望解決 large MFE → net loss。
- 不希望承擔裸腿曝險。
- 市場 regime 不明確或混合。

### 主要風險

- 過早結束可能仍有利的 spread trajectory。
- 雙腿退出存在 fill asymmetry。
- Combined PnL 計算若未扣除 friction，可能高估可鎖利潤。

---

# 3. Non-Negotiable Architecture Rules

## 3.1 Exit Family 必須互斥

同一筆 trade 僅能有一個 Exit Family 擁有下單權。

禁止：

```text
NORMAL says release loser
REVERSE says harvest winner
SPREAD_TRAIL says exit both
```

同一 tick 競爭下單。

---

## 3.2 Entry-Time Freeze

每筆 trade 在建立時凍結：

```text
selected_exit_family
selected_exit_family_version
selected_parameter_profile
selected_parameters_hash
selected_at
selection_source
```

持倉中不得任意切換。

---

## 3.3 Risk Override 不等於 Strategy Switch

持倉後可因以下原因觸發 emergency flatten：

- Broker position mismatch
- Quote stale
- Feed degraded
- Settlement cutoff
- Session close
- Structural risk gate failure

但不可直接：

```text
REVERSE_HARVEST → NORMAL_RELEASE
```

除非未來另有正式研究與 transition ADR。

---

## 3.4 Dashboard 不得直接下單

Dashboard 僅能：

- 顯示 runtime / trade / strategy 狀態。
- 設定下一筆交易的 Exit Family。
- 選擇已核准 Parameter Profile。
- 呼叫 control service。

Dashboard 不得：

- 直接修改 FSM state。
- 直接修改 active trade policy。
- 直接呼叫 Shioaji 下單。
- 直接覆寫 broker truth。
- 直接繞過 OrderManager 或 risk gate。

---

# 4. Phase 0 — Baseline Freeze and Governance

## Goal

在新增策略前，凍結現有 MTS baseline，避免研究結果被後續程式修改污染。

## Tasks

### 0.1 Freeze Current NORMAL_RELEASE

記錄：

```text
Git commit
Engine version
Config version
Dataset contract version
Replay engine version
Build timestamp
```

### 0.2 建立 Exit Family Enum

```python
class ExitFamily(str, Enum):
    NORMAL_RELEASE = "NORMAL_RELEASE"
    REVERSE_HARVEST = "REVERSE_HARVEST"
    SPREAD_PNL_TRAIL = "SPREAD_PNL_TRAIL"
```

### 0.3 建立統一 Exit Policy Protocol

```python
class ExitPolicy(Protocol):
    def evaluate(
        self,
        context: SpreadContext,
    ) -> ExitEvaluation:
        ...
```

### 0.4 建立統一 Decision Contract

```python
@dataclass(frozen=True)
class ExitEvaluation:
    policy: ExitFamily
    action: ExitAction
    legs: tuple[Leg, ...]
    reason: ExitReason
    trigger_value: float | None
    threshold_value: float | None
    diagnostics: ExitDiagnostics
```

### 0.5 新增 Provenance

所有 decision 必須記錄：

```text
policy
policy_version
parameter_profile
parameter_hash
engine_version
git_commit
feature_snapshot_id
event_time
received_at
processed_at
```

## Deliverables

- ADR：MTS Exit Family Architecture
- ExitFamily enum
- ExitPolicy protocol
- ExitEvaluation contract
- Baseline manifest

## Acceptance Criteria

- 現有 NORMAL_RELEASE 行為不變。
- Replay parity 仍為 100%。
- 無 active trade 時可解析 default Exit Family。
- Active trade state 可保存並恢復 Exit Family。

---

# 5. Phase 1 — SPREAD_PNL_TRAIL

## Why First

此模式：

- 與 spread alpha 單位一致。
- 無裸腿曝險。
- FSM 最簡單。
- 最直接處理 PED 問題。
- 對現有資料要求最低。

## 5.1 State Machine

```text
FLAT
→ ENTRY_PENDING
→ SPREAD_ACTIVE
→ SPREAD_TRAIL_ARMED
→ SPREAD_EXIT_PENDING
→ FLAT
```

## 5.2 Core Metrics

```text
combined_net_pnl
combined_peak_pnl
combined_giveback
arming_threshold
trail_threshold
profit_floor
hard_loss_stop
```

## 5.3 Arming Rule

```text
combined_net_pnl >= arm_profit
```

其中：

```text
arm_profit =
    max(
        fixed_arm_profit_twd,
        spread_atr_value × arm_atr_ratio
    )
```

## 5.4 Peak Tracking

```text
combined_peak_pnl =
    max(combined_peak_pnl, combined_net_pnl)
```

## 5.5 Exit Rule

```text
combined_giveback =
    combined_peak_pnl - combined_net_pnl
```

觸發條件：

```text
combined_giveback >= trail_threshold
```

或：

```text
combined_net_pnl <= profit_floor
```

## 5.6 Profit Floor

```text
profit_floor =
    max(
        absolute_profit_floor,
        combined_peak_pnl × retain_ratio
    )
```

## 5.7 Two-Leg Exit

同一個策略 decision 產生兩張 leg-level order intents：

```text
EXIT_NEAR
EXIT_FAR
```

但必須具備：

- One decision ID
- Two child order intent IDs
- Fill reconciliation
- Partial-fill handling
- Emergency completion path

## Tests

```text
test_spread_trail_not_armed_below_threshold
test_spread_peak_updates_monotonically
test_spread_giveback_triggers_exit
test_profit_floor_triggers_exit
test_two_leg_exit_has_single_parent_decision
test_partial_fill_reconciliation
test_restart_recovers_spread_exit_pending
```

## Deliverables

- `SpreadPnlTrailPolicy`
- Config profile
- Replay implementation
- Shadow diagnostics
- Dashboard read-only metrics

## Acceptance Criteria

- Deterministic replay 100%。
- 同一 tick 最多一個 exit decision。
- 雙腿 exit decision 可 crash recovery。
- Net PnL 必須包含 friction estimate。
- 不產生任何單腿策略狀態。

---

# 6. Phase 2 — NORMAL_RELEASE Refactor

## Goal

將現有 NORMAL_RELEASE 從 monitor 中抽離成獨立 policy，保留既有行為。

## Tasks

### 2.1 Extract Policy

```text
NormalReleasePolicy
```

### 2.2 保留現有狀態

```text
SPREAD_ACTIVE
→ RELEASE_PENDING
→ SINGLE_LEG_WARMUP
→ SINGLE_LEG
→ EXIT_PENDING
→ FLAT
```

### 2.3 補齊 PED Telemetry

```text
spread_mfe
trade_mfe
remaining_leg_mfe
exit_pnl
PED
winner_retention_ratio
```

其中：

```text
PED = trade_mfe - final_trade_pnl
```

### 2.4 加入 Spread-Level Profit Protection 研究欄位

先 shadow 記錄：

```text
would_spread_profit_floor_trigger
counterfactual_exit_pnl
```

不直接修改 production 行為。

## Deliverables

- `NormalReleasePolicy`
- Existing behavior parity tests
- PED dataset fields
- Counterfactual hooks

## Acceptance Criteria

- 與 baseline action / leg / reason 100% parity。
- Lifecycle 不變。
- Release fill 前不得進入 SINGLE_LEG。
- Warmup 與 tick dedup 保持有效。

---

# 7. Phase 3 — REVERSE_HARVEST

## Goal

建立完整 Winner Harvest → Loser Recovery FSM。

## 7.1 State Machine

```text
SPREAD_ACTIVE
→ WINNER_TRACKING
→ WINNER_EXIT_PENDING
→ LOSER_RECOVERY_WARMUP
→ LOSER_RECOVERY_ACTIVE
→ LOSER_EXIT_PENDING
→ FLAT
```

## 7.2 Harvest Eligibility

必須全部通過：

```text
spread_upl >= min_spread_profit
winner_peak_upl >= min_winner_peak_profit
abs(loser_upl) <= max_loser_loss_at_harvest
estimated_locked_profit >= min_locked_profit
time_to_forced_flat >= min_recovery_window
quotes healthy
broker position integrity valid
```

## 7.3 Winner Harvest Trigger

```text
winner_drawdown =
    winner_peak_upl - winner_upl
```

```text
harvest_threshold =
    max(
        winner_leg_atr_value × leg_atr_ratio,
        spread_atr_value × spread_atr_ratio,
        fixed_floor
    )
```

觸發需：

```text
drawdown_condition_ticks >= N
AND
drawdown_condition_elapsed_ms >= T
```

## 7.4 Loser Recovery

追蹤：

```text
loser_initial_upl
loser_worst_upl
loser_recovery_amount
loser_recovery_peak
loser_recovery_drawdown
```

Arming：

```text
loser_recovery_amount >= recovery_arm_threshold
```

Trail：

```text
loser_recovery_peak - current_loser_upl
    >= recovery_trail_threshold
```

## 7.5 Mandatory Risk Controls

```text
absolute loser hard stop
trade-level locked-profit floor
recovery timeout
session forced exit
quote stale exit
broker degraded exit
position mismatch exit
```

## Exit Priority

```text
1. Position / broker integrity failure
2. Session forced exit
3. Absolute hard stop
4. Locked-profit floor
5. Quote health failure
6. Recovery trail
7. Recovery timeout
```

## Deliverables

- `ReverseHarvestPolicy`
- Winner tracking FSM
- Loser recovery FSM
- Risk override priority
- Counterfactual replay

## Acceptance Criteria

- Winner fill 前不得進入 recovery。
- 同一 tick 最多 commit 一個 loser exit。
- 所有 naked exposure 都有 hard stop 與 timeout。
- Crash/restart 後可從 broker truth 重建狀態。
- Locked-profit floor 不得有 bypass 路徑。

---

# 8. Phase 4 — Unified Replay and Counterfactual Lab

## Goal

對相同 entry episode 進行多策略 deterministic comparison。

## Required Comparators

```text
A. NORMAL_RELEASE
B. REVERSE_HARVEST
C. SPREAD_PNL_TRAIL
D. Trigger 時雙腿同步平倉
E. Existing production result
```

## Fixed Inputs

每個 episode 必須固定：

```text
entry time
contracts
direction
quantity
entry fills
event sequence
fees
slippage model
session calendar
```

只更換 exit policy。

## Required Metrics

```text
Net PnL
Win rate
Profit factor
Max drawdown
Expected shortfall
PED
Exit efficiency
Holding duration
Naked exposure duration
Locked-profit violation rate
Hard-stop rate
Incremental PnL vs synchronous exit
```

## Replayability Classification

每筆 episode 必須標記：

```text
REPLAYABLE
PARTIALLY_REPLAYABLE
NOT_REPLAYABLE
```

禁止以補值假裝可重播。

## Deliverables

- Unified replay runner
- Policy comparison table
- Sensitivity sweeps
- Experiment registry
- Reproduction hash

## Acceptance Criteria

- 相同 event stream 重跑結果一致。
- Order independence 通過。
- 不使用 future data。
- 每項 counterfactual 有完整 provenance。
- 不完整資料明確拒絕。

---

# 9. Phase 5 — Candidate Entry Data Collection

## Goal

建立 regime selector 與 entry model 所需的完整候選樣本，不只記錄已成交 trade。

## Mandatory Event Types

```text
ENTRY_CANDIDATE
ENTRY_ACCEPTED
ENTRY_REJECTED
NO_TRADE
```

## Feature Snapshot

### Spread Features

```text
spread_z
spread_z_velocity
spread_z_acceleration
spread_atr
spread_slope
spread_realized_volatility
spread_autocorrelation
spread_half_life
mean_crossing_frequency
distance_from_mean
```

### Leg Features

```text
near_return_1m
far_return_1m
near_return_5m
far_return_5m
relative_return
near_atr
far_atr
atr_ratio
relative_volume
lead_lag
```

### Market Features

```text
TAIEX trend
market ATR
market regime
gap
session
time_of_day
event risk flag
```

### Execution Features

```text
bid_ask_spread
quote_age
tick_rate
depth
estimated_slippage
time_to_close
broker_health
feed_health
```

## Critical Rule

所有 feature 必須是：

```text
當時可觀測值
```

不得事後使用完整 trajectory 重算後回填為 entry-time feature。

## Deliverables

- Candidate episode logger
- Feature snapshot contract
- Entry rejection taxonomy
- Data completeness diagnostics

## Acceptance Criteria

- 未成交機會點也有完整樣本。
- 每個 snapshot 有 event-time provenance。
- Feature 缺失時 fail closed。
- 可區分 data invalid 與 legitimate no-trade。

---

# 10. Phase 6 — Regime Assessment

## Goal

產生可解釋、結構化、非單一 label 的 regime assessment。

## Output Contract

```python
@dataclass(frozen=True)
class RegimeAssessment:
    trend_score: float
    mean_reversion_score: float
    breakout_score: float
    spread_stability_score: float
    liquidity_score: float
    confidence: float
    dominant_regime: RegimeType
    evidence: tuple[str, ...]
    warnings: tuple[str, ...]
```

## Three Regime Layers

```text
1. Market Regime
2. Spread Regime
3. Leg Regime
```

## Rule

Regime 只回答：

```text
現在市場像什麼？
```

Regime 不直接回答：

```text
現在立刻下單。
```

## Deliverables

- Regime feature builder
- Rule-based score engine
- Diagnostics
- Shadow dashboard panel

## Acceptance Criteria

- 每個 score 有可追溯 evidence。
- Confidence 低時可輸出 uncertain。
- Regime engine 不直接送單。
- 所有輸入 feature 均來自 entry-time snapshot。

---

# 11. Phase 7 — Strategy Eligibility and Entry Decision

## Goal

將 regime、strategy suitability、entry signal、cost 與 risk 分離。

## Decision Pipeline

```text
Data Quality
→ Regime Assessment
→ Strategy Eligibility
→ Entry Signal
→ Expected Edge
→ Risk Gate
→ Execution Gate
→ Selection
```

## Strategy Scores

```text
normal_score
reverse_score
spread_trail_score
```

選擇需同時滿足：

```text
minimum_strategy_score
minimum_confidence
minimum_score_margin
```

若未滿足：

```text
NO_TRADE
```

## Output Contract

```python
@dataclass(frozen=True)
class StrategySelectionResult:
    selected_family: ExitFamily | None
    decision: SelectionDecision
    normal_score: float
    reverse_score: float
    spread_trail_score: float
    confidence: float
    score_margin: float
    eligible_families: tuple[ExitFamily, ...]
    rejected_families: dict[ExitFamily, tuple[str, ...]]
    regime_snapshot_id: str
    feature_snapshot_id: str
```

## Valid Decisions

```text
SELECT
NO_TRADE
INSUFFICIENT_CONFIDENCE
DATA_INVALID
LIQUIDITY_REJECTED
RISK_REJECTED
EDGE_REJECTED
```

## Deliverables

- Strategy eligibility matrix
- Entry signal evaluator
- Expected edge evaluator
- Selection result contract
- Shadow selector

## Acceptance Criteria

- 不強迫三選一。
- `NO_TRADE` 為一級正式結果。
- Strategy family 只在 entry 時凍結。
- Runner-up 與 score margin 有記錄。
- Cost / slippage 不通過時不得進場。

---

# 12. Phase 8 — Dashboard Control Plane

## Goal

安全地控制下一筆交易 Exit Family，並顯示 active trade 與 shadow selector。

## Dashboard Panels

```text
1. Runtime Identity
2. Active Trade
3. Next Trade Configuration
4. Exit-Family-Specific Metrics
5. Regime / Strategy Scores
6. Counterfactual Results
7. Audit / Diagnostics
8. Emergency Controls
```

## Runtime Identity

必須顯示：

```text
Host
Role
Environment
Repo Path
Git Commit
Repo Dirty State
Config Path
State File Path
Execution Mode
Broker Health
Feed Health
```

目的是避免無法判斷修改或控制發生在 Air4 還是 Mini。

## Active vs Next Trade

必須分開顯示：

```text
Active Trade Exit Family
Next Trade Exit Family
```

規則：

```text
Active Trade Policy = immutable
Next Trade Policy = editable
```

## Runtime Control File

建議：

```text
runtime/mts_strategy_control.json
```

包含：

```text
schema_version
ticker
next_trade_exit_family
parameter_profile
target_host
target_role
environment
updated_at
updated_by
git_commit
config_hash
```

## Control Authority

```text
1. Active trade state
2. Runtime control file
3. Static config
4. Safe default
```

## Production Permissions

### Replay

允許：

- 切換 policy
- 切換 profile
- 跑 counterfactual
- 匯出結果

### Paper

允許：

- 設定下一筆 Exit Family
- 設定 approved profile

### Live

僅在以下條件允許修改下一筆：

```text
FLAT
broker flat
open orders zero
preflight passed
host / role authorized
```

## Deliverables

- Runtime control service
- Atomic writer
- Audit log
- Dashboard panels
- Host / role gate

## Acceptance Criteria

- Dashboard 不直接寫 active trade state。
- Dashboard 不直接呼叫 broker。
- Air4 不得控制 Mini runtime。
- Runtime control 使用 atomic write。
- 所有變更有 audit event。
- PM2 restart 後設定可恢復。

---

# 13. Phase 9 — Shadow Selection

## Goal

讓 selector 在線上持續產生建議，但不控制下單。

每個候選 entry 記錄：

```text
regime scores
strategy scores
recommended family
confidence
score margin
actual decision
actual selected family
```

交易結束後，執行三路 counterfactual，建立：

```text
recommended family
best realized family
PnL delta
risk delta
selection regret
```

## Deliverables

- Shadow recommendation logger
- Outcome attribution
- Regret analysis
- Dashboard recommendation panel

## Acceptance Criteria

- Shadow selector 不可影響實際 order flow。
- 可明確比較 recommendation 與最佳 counterfactual。
- 不以 regime label accuracy 作為主要成功指標。
- 主要指標為 risk-adjusted PnL improvement。

---

# 14. Phase 10 — Validation

## Time-Based Split

禁止隨機切分。

應採：

```text
Calibration Period
Validation Period
Untouched Test Period
```

## Sample Guidance

初步研究建議：

```text
每個主要 regime 30–50 個可重播 episode
總體至少 150–300 個候選 episode
```

但不可把固定筆數當成唯一門檻。

需同時檢查：

- Regime coverage
- Session coverage
- ATR coverage
- Settlement coverage
- Tail-event coverage
- Data completeness

## Primary Validation Questions

```text
1. SPREAD_PNL_TRAIL 是否穩定降低 PED？
2. REVERSE_HARVEST 的額外收益是否補償 naked tail risk？
3. NORMAL_RELEASE 是否只在特定 continuation regime 優勢明顯？
4. Selector 是否優於固定使用單一策略？
5. Selector 是否優於 always NO_TRADE / conservative baseline？
```

## Acceptance Criteria

- Out-of-sample 改善。
- Tail loss 不顯著惡化。
- Selector 有穩定 score margin。
- 成本後 PnL 仍為正。
- Strategy selection 不依賴 future leakage。

---

# 15. Phase 11 — Paper Auto-Selection

## Preconditions

以下全部成立才可啟用：

```text
Replay deterministic
Shadow selector stable
Out-of-sample pass
Risk controls complete
Dashboard audit complete
Crash recovery complete
```

## Initial Limits

```text
TMF only
1 spread unit
one active MTS trade at a time
daily loss cap
no settlement-day auto-selection
no live mode
```

## Deliverables

- Paper auto-selector
- Daily diagnostics
- Automatic fallback to NO_TRADE
- Selector kill switch

## Acceptance Criteria

- Selector failure → NO_TRADE。
- Data invalid → NO_TRADE。
- Broker degraded → NO_TRADE。
- Active trade Exit Family 不變。
- 所有 paper decisions 可 replay。

---

# 16. Phase 12 — Limited Live

## Preconditions

- Paper 樣本涵蓋多 regime。
- Tail loss 可接受。
- No unresolved execution incident。
- Host / role / broker preflight 完整。
- Operator audit 與 rollback plan 完成。

## Initial Scope

```text
TMF only
1 unit
approved profiles only
manual enable per session
daily loss cap
no automatic profile editing
no strategy switching mid-trade
```

## Rollback Conditions

以下任一發生立即停用 auto-selection：

```text
position mismatch
duplicate order intent
state recovery failure
unexpected naked exposure
selector provenance missing
counterfactual parity failure
live loss beyond risk envelope
```

---

# 17. Configuration Model

```yaml
mts:
  exit_family_mode: MANUAL
  # MANUAL
  # SHADOW_SELECTOR
  # PAPER_AUTO_SELECTOR
  # LIVE_AUTO_SELECTOR

  default_exit_family: NORMAL_RELEASE

  profiles:
    normal_release_v1:
      family: NORMAL_RELEASE
      release_threshold_mode: ATR_DYNAMIC
      release_atr_ratio: 1.0
      warmup_ms: 800
      warmup_ticks: 2

    reverse_harvest_v1:
      family: REVERSE_HARVEST
      harvest_atr_ratio: 1.0
      harvest_confirm_ticks: 2
      harvest_confirm_ms: 500
      loser_hard_stop_twd: -1800
      recovery_timeout_seconds: 900
      retain_ratio: 0.25

    spread_pnl_trail_v1:
      family: SPREAD_PNL_TRAIL
      arm_profit_twd: 800
      arm_atr_ratio: 0.8
      trail_atr_ratio: 0.8
      fixed_trail_floor_twd: 200
      retain_ratio: 0.4
      hard_stop_twd: -1500

  selector:
    enabled: false
    minimum_confidence: 0.60
    minimum_strategy_score: 0.65
    minimum_score_margin: 0.10
    fallback: NO_TRADE
```

---

# 18. Required Telemetry

## Common

```text
trade_id
ticker
contracts
entry_time
entry_direction
entry_prices
selected_exit_family
selected_profile
selection_source
selection_confidence
feature_snapshot_id
regime_snapshot_id
```

## NORMAL_RELEASE

```text
release_leg
release_threshold
release_loss
remaining_leg_mfe
remaining_leg_mae
remaining_leg_exit
PED
winner_retention_ratio
```

## REVERSE_HARVEST

```text
winner_leg
winner_peak_upl
winner_drawdown
winner_realized_pnl
loser_initial_upl
loser_worst_upl
recovery_peak
recovery_duration
locked_profit_floor
naked_exposure_duration
```

## SPREAD_PNL_TRAIL

```text
combined_net_pnl
combined_peak_pnl
combined_giveback
arm_threshold
trail_threshold
profit_floor
two_leg_exit_latency
fill_asymmetry
```

## Selector

```text
normal_score
reverse_score
spread_trail_score
confidence
runner_up
score_margin
eligible_families
reject_reasons
final_decision
```

---

# 19. Agent Execution Rules

## Before Editing

Agent 必須先輸出：

```text
HOSTNAME
REPO_PATH
CURRENT_BRANCH
GIT_COMMIT
GIT_STATUS
TARGET_PHASE
TARGET_FILES
```

並確認：

```text
正在修改 Air4 還是 Mini
是否為研究環境或 broker executor
是否存在未同步 commit
```

## Coding Rules

- 不得同時在 Air4 與 Mini 修改同一功能。
- 修改前先同步 branch。
- 不得直接修改 active trade state。
- 不得新增隱式 strategy switch。
- 不得使用 wall clock 取代 event time。
- 不得吞掉 exception。
- 不得將 data missing 視為 false signal。
- 不得用 broad fallback 掩蓋 provenance failure。
- 所有新 decision 必須可 replay。
- 所有 write 必須 atomic。
- 所有 broker action 必須經 OrderManager。
- 所有 state transition 必須由單一入口 commit。

## Completion Report

Agent 完成後必須回報：

```text
Host
Branch
Commit
Files changed
Behavior changed
Behavior intentionally unchanged
Tests run
Test results
Replay results
Known limitations
Deployment target
Sync status
Rollback command
```

---

# 20. Final Program Sequence

建議實作順序：

```text
Phase 0  Baseline Freeze
Phase 1  SPREAD_PNL_TRAIL
Phase 2  NORMAL_RELEASE Refactor
Phase 3  REVERSE_HARVEST
Phase 4  Unified Replay
Phase 5  Candidate Entry Logging
Phase 6  Regime Assessment
Phase 7  Strategy Eligibility + Entry Decision
Phase 8  Dashboard Control Plane
Phase 9  Shadow Selection
Phase 10 Validation
Phase 11 Paper Auto-Selection
Phase 12 Limited Live
```

不可跳過：

```text
Replay
Data Collection
Shadow
Out-of-Sample Validation
```

直接進入自動 live selection。

---

# 21. Strategic Conclusion

此計畫的核心不是建立一個「會猜 regime 的分類器」，而是建立一個可驗證的策略決策系統：

```text
三種清楚分離的 Exit Family
+
完整候選 entry dataset
+
可解釋 regime assessment
+
deterministic counterfactual replay
+
NO_TRADE fail-closed
```

第一優先應完成：

```text
SPREAD_PNL_TRAIL
+
Unified Replay
+
Candidate Entry Feature Logging
```

因為在沒有三種 exit policy 的可靠 counterfactual outcome 前，regime selector 沒有可信的學習目標。
