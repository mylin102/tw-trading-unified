---
phase: 1000-implement-truthful-live-theta-execution-and-complex-order-or
plan: 03
subsystem: api
tags: [shioaji, combo-recovery, theta-gang, order-manager, pytest]
requires:
  - phase: 1000-02
    provides: pending TXO-COMBO lifecycle orders for live theta submit flows
provides:
  - broker combo polling and startup recovery before ledger fallback
  - combo fill deduplication tied to recovered combo deal identity
  - theta runtime mutation only after confirmed combo fill truth
affects: [1000-04, 1000-05]
tech-stack:
  added: []
  patterns: [broker-first combo recovery, combo status reconciliation, restart-safe theta state rehydration]
key-files:
  created: [tests/test_combo_recovery.py]
  modified: [tests/test_order_lifecycle/test_order_state_vs_deal_state.py, strategies/options/options_engine/engine/broker_adapter.py, core/order_management/order_manager.py, strategies/options/live_options_squeeze_monitor.py]
key-decisions:
  - "Combo startup recovery now loads combo broker status before ordinary order/ledger fallback."
  - "Recovered combo fills are deduplicated by one aggregated combo identity and only mutate theta runtime after broker-confirmed fill truth."
  - "Open combo recovery rebuilds pending_theta_combo from lifecycle orders so restart never resubmits the broker order."
patterns-established:
  - "Pattern 1: recover_from_api can reconcile broker combo snapshots into one TXO-COMBO lifecycle order with raw payload audit history."
  - "Pattern 2: live options refresh polls combo broker APIs before timeout/retry logic so broker truth wins over local assumptions."
requirements-completed: [RECN-01, RECN-02, RECN-03, EXEC-03]
duration: 20m
completed: 2026-04-21
---

# Phase 1000 Plan 03: Reconcile combo fills and restart recovery from broker combo APIs before any ledger fallback Summary

**Broker combo polling and restart recovery now reconcile TXO-COMBO lifecycle truth before ledger fallback, dedupe recovered combo fills, and mutate theta runtime only after confirmed broker fill state.**

## Performance

- **Duration:** 20 min
- **Started:** 2026-04-21T10:51:00Z
- **Completed:** 2026-04-21T11:10:55Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- Added combo recovery regressions for startup precedence, partial fills, cancel/reject, and no-resubmit restart behavior.
- Added broker adapter and order-manager combo reconciliation helpers that preserve source, reason, and raw payload audit history.
- Wired live theta runtime/startup recovery to poll combo broker APIs first and only open/close local theta state after broker-confirmed combo fill truth.

## Task Commits

Each task was committed atomically:

1. **Task 1: Add combo recovery and reconciliation regressions** - `f89067a` (test)
2. **Task 2: Implement combo polling/recovery and apply fill truth only from broker combo status** - `725a67e` (feat)

## Files Created/Modified
- `tests/test_combo_recovery.py` - Startup recovery regressions for combo precedence over ledger and restart-safe pending combo restoration.
- `tests/test_order_lifecycle/test_order_state_vs_deal_state.py` - Combo partial fill, cancel/reject, and duplicate recovery assertions.
- `strategies/options/options_engine/engine/broker_adapter.py` - Added combo status listing helper that refreshes broker combo state before reads.
- `core/order_management/order_manager.py` - Added combo snapshot reconciliation, recovered combo order creation, fill deduping, and combo-aware API recovery.
- `strategies/options/live_options_squeeze_monitor.py` - Added combo polling/startup orchestration, pending combo rebuilds, and theta state mutation gated on confirmed combo fills.

## Decisions Made
- Treat broker combo APIs as the first recovery surface for live theta, with ledger rebuild remaining a last-resort degraded fallback.
- Reconcile one broker combo order into one lifecycle order and dedupe replayed combo fills using an aggregated combo identity instead of fabricating leg lifecycle rows.
- Rebuild `pending_theta_combo` from recovered broker-truth lifecycle orders so restart logic blocks duplicate submit attempts automatically.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 - Blocking] Preserved legacy live-order recovery when a broker stub lacks combo APIs**
- **Found during:** Task 2 verification
- **Issue:** Existing recovery tests used a broker double with only `list_open_orders/list_trades`, and the new combo-first recovery path failed when combo methods were absent.
- **Fix:** Made combo recovery probes conditional so classic single-order recovery still succeeds while combo-capable brokers get combo-first polling.
- **Files modified:** `strategies/options/live_options_squeeze_monitor.py`
- **Verification:** `python3 -m pytest tests/test_order_lifecycle/test_order_state_vs_deal_state.py::test_recover_live_orders_from_broker_populates_order_manager -v`, `python3 -m pytest tests/ -v`
- **Committed in:** `725a67e`

---

**Total deviations:** 1 auto-fixed (1 blocking)
**Impact on plan:** The deviation preserved backward-compatible broker recovery while keeping combo-first truth semantics. No scope creep.

## Issues Encountered
- Full-suite verification exposed an older recovery test double that did not implement combo APIs; compatibility handling resolved it without weakening combo-first behavior.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Wave 3 is complete and safe for wave 4 dashboard/operator-truth work: combo restart and runtime reconciliation now prefer broker truth and avoid fake local fills.
- Dashboard work can now read stable `TXO-COMBO` lifecycle rows with audit source/reason history and pending-versus-filled truth.

---
*Phase: 1000-implement-truthful-live-theta-execution-and-complex-order-or*
*Completed: 2026-04-21*

## Self-Check: PASSED
