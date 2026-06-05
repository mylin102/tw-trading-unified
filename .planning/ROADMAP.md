# Roadmap: tw-trading-unified

## Overview

Milestone v1.2 focuses on adaptive strategy optimization, specifically transforming the `tmf_spread` strategy into a volatility-adaptive system. Milestone v1.1 hardens execution reliability across futures/options lifecycle, reconciliation, and dashboard visibility.

## Phases

- [ ] **Phase 12-1: ATR Adaptive Verification (v1.2)** - Finalize and verify ATR-based improvements for `tmf_spread` with comprehensive tests and dashboard visibility.
- [ ] **Phase 1: Lifecycle Truth Contract (v1.1)** - Normalize futures/options paper/live execution around linked intent, order, and deal records.
- [ ] **Phase 2: Broker Reconciliation & Restart Recovery (v1.1)** - Rebuild broker-truth state after callback gaps or process restarts without duplicate execution.
- [ ] **Phase 3: Operator Lifecycle Visibility (v1.1)** - Expose lifecycle truth in dashboard views without redesigning the dashboard.
- [ ] **Phase 4: V-Model Validation & Runtime Hardening (v1.1)** - Lock the lifecycle redesign with regression proof and runtime compatibility checks.

## Phase Details

### Phase 12-1: ATR Adaptive Verification
**Goal**: TMF spread strategy successfully adapts its entry, stop, and trail logic to market volatility (ATR) with verified correctness and operator visibility.
**Depends on**: Nothing (parallel to v1.1 hardening)
**Requirements**: ADAPT-01, ADAPT-02, ADAPT-03, VMDL-01, VIEW-02
**Success Criteria** (what must be TRUE):
  1. Strategy skips entry in low-volatility environments based on `min_atr` gate.
  2. Release stops and trailing exits scale dynamically with ATR using configured multipliers.
  3. Dynamic thresholds are visible to operators in the MTS dashboard via state JSON.
  4. 100% coverage of adaptive logic in unit tests, including safety floors (5pt/10pt).
**Plans**: 1 plan

Plans:
- [ ] PHASE-1-VERIFICATION.md — Lock adaptive config, implement ATR unit tests, and verify dashboard visibility

### Phase 1: Lifecycle Truth Contract
**Goal**: Futures/options paper/live execution uses one lifecycle model where intent, order, and deal truth stay linked and positions update only from confirmed deals.
**Depends on**: Nothing (first phase)
**Requirements**: EXEC-01, EXEC-02, EXEC-03
**Success Criteria** (what must be TRUE):
  1. Operator can trace every futures/options paper/live trade through linked intent, order, and deal records from submission to terminal state.
  2. Lifecycle records clearly distinguish accepted, partial fill, full fill, cancel, and reject outcomes instead of collapsing them into one trade result.
  3. Position size and cost basis change only after confirmed deal records arrive; submit, pending, cancelled, and rejected orders do not mutate held position.
**Plans**: 3 plans

Plans:
- [x] 01-01-PLAN.md — Define traceable lifecycle IDs and compatibility exports across shared order/fill contracts
- [x] 01-02-PLAN.md — Separate order-state transitions from deal-driven fill truth in the shared manager and options callback path
- [x] 01-03-PLAN.md — Anchor futures position and cost-basis updates to confirmed deal handling and `PaperTrader.position`

### Phase 2: Broker Reconciliation & Restart Recovery
**Goal**: The system can rebuild broker-truth lifecycle state after callback gaps or restarts without re-submitting or losing active execution.
**Depends on**: Phase 1
**Requirements**: RECN-01, RECN-02, RECN-03
**Success Criteria** (what must be TRUE):
  1. When callback gaps occur, the system refreshes broker status and restores the latest order and fill state instead of relying on stale local assumptions.
  2. After process restart, open orders, fills, and position links are recovered without duplicate submissions or orphaned execution state.
  3. Each lifecycle state transition is reviewable with timestamp, source, and reason so operators can audit how local truth was rebuilt.
**Plans**: 3 plans

Plans:
- [ ] 02-01-PLAN.md — Build the shared broker reconciliation engine and normalized lifecycle audit entries
- [ ] 02-02-PLAN.md — Rehydrate futures/options active orders, fills, and pending strategy state safely after restart
- [ ] 02-03-PLAN.md — Lock callback-gap recovery and duplicate-submit prevention with regression gates

### Phase 3: Operator Lifecycle Visibility
**Goal**: Operators can supervise execution from dashboard surfaces that reflect the same lifecycle truth as the trading engine.
**Depends on**: Phase 2
**Requirements**: VIEW-01, VIEW-02, VIEW-03, VIEW-04
**Success Criteria** (what must be TRUE):
  1. Operator can see entry orders, exit orders, and fills as separate lifecycle records for the same trade.
  2. Operator can see cost basis, realized PnL, and unrealized PnL that match the underlying lifecycle and position truth.
  3. Night-session orders and deals appear under the correct trading day in dashboard and review surfaces.
  4. Operator can see when local lifecycle state is pending reconciliation or disagrees with broker state.
**Plans**: 3 plans
**UI hint**: yes

Plans:
- [ ] 03-01: Expose separate entry, exit, and fill lifecycle records in dashboard views
- [ ] 03-02: Align dashboard cost basis and PnL displays with lifecycle-derived truth
- [ ] 03-03: Surface trading-day mapping and reconciliation mismatch status for operators

### Phase 4: V-Model Validation & Runtime Hardening
**Goal**: The lifecycle redesign is proven safe through regression coverage and milestone verification of the current runtime path.
**Depends on**: Phase 3
**Requirements**: VMDL-01, VMDL-02
**Success Criteria** (what must be TRUE):
  1. Regression tests exercise partial fill, cancel/reject, restart recovery, and trading-day mapping across futures/options lifecycle flows.
  2. A milestone verification run shows `main.py` still orchestrates the current runtime path correctly with lifecycle changes enabled.
  3. A milestone verification run shows the 8500 dashboard still supports its core operator supervision path with the new lifecycle model in place.
**Plans**: 2 plans
**UI hint**: yes

Plans:
- [ ] 04-01: Add V-model regression coverage for lifecycle edge cases and recovery behavior
- [ ] 04-02: Run milestone verification against `main.py` and the 8500 dashboard core path

## Progress

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 12-1. ATR Verification | 0/1 | In Progress | - |
| 1. Lifecycle Truth Contract | 3/3 | Complete | 2026-04-21 |
| 2. Broker Reconciliation | 0/3 | Not started | - |
| 3. Operator Visibility | 0/3 | Not started | - |
| 4. V-Model Validation | 0/2 | Not started | - |
| 1000. Live Theta Combo | 5/5 | Complete | 2026-04-21 |

## Backlog

### Phase 999.1: Stock lifecycle extension on shared order management (BACKLOG)

**Goal:** [Captured for future planning]
**Requirements:** TBD
**Plans:** 0 plans

Plans:
- [ ] TBD (promote with /gsd-review-backlog when ready)

### Phase 1000: Implement truthful live theta execution and complex-order order path

**Goal:** Live ThetaGang vertical spreads submit and reconcile through truthful Shioaji combo orders so live mode cannot silently simulate paper theta fills, combo margin/cost truth matches spread reality, and operator surfaces clearly distinguish broker combo truth from paper/ledger fallbacks.
**Requirements**: EXEC-01, EXEC-02, EXEC-03, RECN-01, RECN-02, RECN-03, VIEW-01, VIEW-02, VIEW-04, VMDL-01, VMDL-02
**Depends on:** Phase 999
**Success Criteria** (what must be TRUE):
  1. Live `bull_put_spread` and `bear_call_spread` entries/exits use Shioaji combo APIs and never route through `_record_paper_order(...)`.
  2. Unsupported live theta strategies (`iron_condor`, `short_strangle`) fail fast with explicit operator/audit messaging instead of falling back to paper lifecycle rows.
  3. Broker combo lifecycle, restart recovery, and dashboard/order-export truth surfaces preserve one combo order as the broker-truth unit and visibly label non-broker fallbacks.
  4. V-model tests and runtime smoke checks prove the combo path, dashboard truth labels, and current runtime startup path remain regression-safe.
**Plans:** 5/5 plans complete

Plans:
- [x] 1000-01-PLAN.md — Add combo-order adapter and lifecycle contract support for broker-truth theta spreads
- [x] 1000-02-PLAN.md — Submit truthful live theta vertical spreads and gate unsupported live theta strategies
- [x] 1000-03-PLAN.md — Reconcile combo fills and restart recovery from broker combo APIs before any ledger fallback
- [x] 1000-04-PLAN.md — Expose broker combo versus paper/ledger truth clearly in dashboard theta/order surfaces
- [x] 1000-05-PLAN.md — Lock the combo path with V-model regressions and startup/dashboard smoke coverage
