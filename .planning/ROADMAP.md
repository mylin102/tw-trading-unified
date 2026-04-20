# Roadmap: tw-trading-unified

## Overview

Milestone v1.1 hardens execution reliability in four behavior-safe steps: first the shared lifecycle contract for futures/options paper/live execution, then broker reconciliation and restart recovery, then minimal operator visibility on the 8500 dashboard, and finally V-model validation that proves the refactor preserves the current runtime path. This keeps `main.py` and the existing dashboard stable while broker-truth execution state becomes recoverable and test-locked.

## Phases

- [ ] **Phase 1: Lifecycle Truth Contract** - Normalize futures/options paper/live execution around linked intent, order, and deal records.
- [ ] **Phase 2: Broker Reconciliation & Restart Recovery** - Rebuild broker-truth state after callback gaps or process restarts without duplicate execution.
- [ ] **Phase 3: Operator Lifecycle Visibility** - Expose lifecycle truth in dashboard views without redesigning the dashboard.
- [ ] **Phase 4: V-Model Validation & Runtime Hardening** - Lock the lifecycle redesign with regression proof and runtime compatibility checks.

## Phase Details

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
- [ ] 01-01-PLAN.md — Define traceable lifecycle IDs and compatibility exports across shared order/fill contracts
- [ ] 01-02-PLAN.md — Separate order-state transitions from deal-driven fill truth in the shared manager and options callback path
- [ ] 01-03-PLAN.md — Anchor futures position and cost-basis updates to confirmed deal handling and `PaperTrader.position`

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
| 1. Lifecycle Truth Contract | 0/3 | Not started | - |
| 2. Broker Reconciliation & Restart Recovery | 0/3 | Not started | - |
| 3. Operator Lifecycle Visibility | 0/3 | Not started | - |
| 4. V-Model Validation & Runtime Hardening | 0/2 | Not started | - |
