# Requirements: tw-trading-unified

**Defined:** 2026-04-20
**Core Value:** The system must preserve broker-truth execution state and capital safety so trading decisions and operator actions are based on correct, recoverable lifecycle data.

## v1 Requirements

### Execution Lifecycle

- [x] **EXEC-01**: Operator can trace every futures/options, paper/live trade through linked intent, order, and deal records
- [x] **EXEC-02**: System can distinguish accepted, partial fill, full fill, cancel, and reject states without collapsing them into a single trade outcome
- [x] **EXEC-03**: Position and cost basis update only from confirmed deal data, not optimistic order placement state

### Reconciliation & Recovery

- [x] **RECN-01**: System can rebuild broker-truth lifecycle state through `update_status()` or equivalent reconciliation after callback gaps
- [x] **RECN-02**: After process restart, futures/options paper-live execution can recover open orders, fills, and position links without duplicate execution
- [x] **RECN-03**: Every lifecycle state transition records an audit trail with timestamp, source, and reason

### Operator Visibility

- [x] **VIEW-01**: Operator can see entry and exit orders separately from fills in dashboard lifecycle views
- [x] **VIEW-02**: Operator can see cost basis, realized PnL, and unrealized PnL from the same lifecycle truth
- [ ] **VIEW-03**: Night-session orders and deals map to the correct trading day in dashboard and review surfaces
- [x] **VIEW-04**: Operator can see when local lifecycle state disagrees with broker state or is pending reconciliation

### V-Model Verification

- [ ] **VMDL-01**: Regression tests cover partial fill, cancel/reject, restart recovery, and trading-day mapping across futures/options lifecycle flows
- [ ] **VMDL-02**: Milestone verification proves lifecycle refactor does not break current `main.py` orchestration or the 8500 dashboard's core runtime path

## v2 Requirements

### Platform Evolution

- **PLAT-01**: Execution stack can move from Python Shioaji integration to a Rust-backed implementation
- **PLAT-02**: Runtime can be deployed to GCP with the same lifecycle guarantees

### UX Expansion

- **UX-01**: Dashboard lifecycle surfaces get a broader redesign beyond the minimum execution-truth views

## Out of Scope

| Feature | Reason |
|---------|--------|
| Strategy tuning or new alpha models | This milestone is about execution correctness, not signal changes |
| Stock-system architecture changes | The current milestone is scoped to futures/options execution reliability |
| Full dashboard redesign | Only lifecycle views needed for execution truth are in scope |
| Rust Shioaji rewrite | Valuable later, but adds platform risk before the lifecycle model is stable |
| GCP migration | Deferred until local runtime and supervision are boring |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| EXEC-01 | Phase 1 | Complete |
| EXEC-02 | Phase 1 | Complete |
| EXEC-03 | Phase 1 | Complete |
| RECN-01 | Phase 2 | Complete |
| RECN-02 | Phase 2 | Complete |
| RECN-03 | Phase 2 | Complete |
| VIEW-01 | Phase 3 | Complete |
| VIEW-02 | Phase 3 | Complete |
| VIEW-03 | Phase 3 | Pending |
| VIEW-04 | Phase 3 | Complete |
| VMDL-01 | Phase 4 | Pending |
| VMDL-02 | Phase 4 | Pending |

**Coverage:**
- v1 requirements: 12 total
- Mapped to phases: 12
- Unmapped: 0 ✓

---
*Requirements defined: 2026-04-20*
*Last updated: 2026-04-20 after roadmap creation*
