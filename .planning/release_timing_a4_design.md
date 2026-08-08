# Release Timing / Reversal Controller A4(theta) — 設計（RESEARCH ONLY）

**狀態**: DESIGN + RED matrix（codex 2026-08-08 授權新獨立 workstream;
先設計 + RED, 審查後才 implement/run）
**硬限制**: 零 production 變更（strategy/config/monitor/dashboard/deploy
全禁）; **不改既有 A0/A1/A2/A3 定義**; research namespace 分離
（scripts/research/release_timing_a4/ + tests/research/test_release_timing_a4.py）
**重用**: phase_transition_replay 的 immutable stream / state-clone /
evidence tiers / reconciliation / manifest 契約 — **禁止 ad hoc data
loading**。

## 1. Hypothesis（僅測這個）

fixed/ATR breach arms 的 **risk**（reversal timing 的價值）—
A4(theta) **本身不執行 release**，只輸出決策供對照。

## 2. State machine

NORMAL →（threshold breach）→ **RELEASE_ARMED** →（observe only
past events）→ reversal / new decision point → 依 deterministic policy 選:
- **R0** release-now / **R1** remain-SPREAD / **R2** atomic-exit /
  **R3** continue-wait

**Safety escapes**（結束 ARMED）: combined-loss floor / max adverse
excursion / max wait / quote|data-quality failure / lifecycle|pending
conflict。

## 3. Persist 每 breach

- **breach snapshot**: ts, loss-leg pnl, combined net, price, spread, z,
  ATR, state clone hash, event/replay sequence, config version
- **running after-breach extrema**（僅隨 events 到達更新）: worst leg /
  combined, adverse price/spread/z, MAE, recovery, elapsed
- **termination cause**

## 4. Clone / causality

- clone complete state **immediately BEFORE breach event**（never
  actual-release future state）
- reversal detection **online/causal** from immutable event stream;
  running extrema 只隨 events 更新 — **無 future extreme/timestamp
  selection**

## 5. A4 families（pre-registered, 無 ex-post selection）

- **A4-Leg**: recovery distances grid（loss leg）
- **A4-Combined**: recovery-from-post-breach trough grid
- **A4-Spread/Z**: |z| reversal / velocity sign+confirmation /
  acceleration-deceleration
- 外加: max-wait / combined-loss-floor
- 順序: single-factor sweeps → 小 factorial subset（顯式宣告）;
  **回報每個 theta**; no winner-by-max-PnL

## 6. Branch control（R3 防 hindsight/combinatorial）

- deterministic tree budget + state key; R3 的每個下一決策點有**固定
  next level / max wait / safety**; 所有 branches 共享同一 event sequence

## 7. Execution-quality tiers（每個 A4 決策輸出）

EXECUTABLE_BBO / BOUNDED_PROXY / MARK_PROXY / NOT_AVAILABLE —
**無 historical BBO 永不 claim executable**。

## 8. 輸出

absolute PnL / pairwise deltas / matrix / MAE / tail / recovery rate /
exposure / controller / termination; required reports:
immediate-release vs A4 paired delta / meaningful recovery rate /
action distribution after recovery / bad-execution+adverse-extreme
reduction / tail+MAE cost of waiting / metric stability / outlier
leave-one-out / session+volatility+z regime。
Parameters（含 thresholds）**per event 從 deployed config resolve**,
否則 NOT_AVAILABLE。

## 9. RED tests（獨立 collection）

breach arms ≠ releases / pre-breach clone completeness / no look-ahead
extreme+reversal / identical event ordering across branches / 每個
safety escape / R3 deterministic bounded progression / pending
conflict+data quality / metric attribution 分離 / theta sweep 無
selection / evidence tier+manifest。

## 10. 交付順序

1. 本設計 + skeletal package + RED matrix（本輪）→ codex 審查 →
2. implementation（research only）→ 3. run + artifact → 4. 報告。
