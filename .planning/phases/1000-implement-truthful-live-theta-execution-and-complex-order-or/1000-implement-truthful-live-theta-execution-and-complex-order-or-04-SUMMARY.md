---
phase: 1000-implement-truthful-live-theta-execution-and-complex-order-or
plan: 04
subsystem: ui
tags: [streamlit, dashboard, options, combo, truth-source, pnl]
requires:
  - phase: 1000-01
    provides: combo lifecycle truth metadata persisted on orders
  - phase: 1000-02
    provides: live theta combo submit path for supported spreads
  - phase: 1000-03
    provides: broker-combo-first recovery and pending combo truth
provides:
  - explicit broker_combo / paper_strategy / ledger_rebuilt dashboard labels
  - spread-aware combo valuation and leg summaries in the options lifecycle panel
  - degraded fallback messaging when ledger rebuild is used
affects: [1000-05, dashboard verification, operator supervision]
tech-stack:
  added: []
  patterns: [truth-source-first dashboard rendering, combo-leg spread valuation reuse]
key-files:
  created: [core/dashboard_positions.py, tests/test_options_combo_dashboard_truth.py]
  modified: [ui/dashboard.py, tests/test_dashboard_positions.py, tests/test_options_order_lifecycle_dashboard.py]
key-decisions:
  - "Infer operator-facing truth badges from persisted truth_source plus ledger rebuild state instead of showing one global theta disclaimer."
  - "Value broker combo rows from combo_legs metadata so dashboard spread PnL does not depend on a ledger open-position match."
patterns-established:
  - "Dashboard lifecycle rows must show truth_source explicitly when broker truth can degrade."
  - "Combo lifecycle valuation uses spread metadata before any single-leg premium fallback."
requirements-completed: [VIEW-01, VIEW-02, VIEW-04]
duration: 7min
completed: 2026-04-21
---

# Phase 1000 Plan 04: Expose broker combo versus paper/ledger truth clearly in dashboard theta/order surfaces Summary

**Streamlit options lifecycle rows now disclose broker combo truth versus paper or ledger fallback and price combo spreads with persisted leg metadata.**

## Performance

- **Duration:** 7 min
- **Started:** 2026-04-21T11:17:47Z
- **Completed:** 2026-04-21T11:23:26Z
- **Tasks:** 2
- **Files modified:** 5

## Accomplishments
- Added regression coverage for broker_combo, paper_strategy, and ledger_rebuilt dashboard truth labeling.
- Reused spread-aware valuation for combo lifecycle rows, including broker combo rows that only have combo metadata.
- Surfaced combo leg summaries and degraded ledger-rebuild captions while limiting the paper-theta disclaimer to non-broker truth rows.

## Task Commits

1. **Task 1: Add dashboard truth-label regressions for combo, paper, and ledger fallback rows** - `acbc6ee` (test)
2. **Task 2: Implement combo truth-source UI and spread-aware lifecycle display** - `e6358a9` (feat)

## Files Created/Modified
- `core/dashboard_positions.py` - truth badge helpers, combo leg summaries, and spread-aware combo valuation helpers.
- `ui/dashboard.py` - lifecycle truth-source column, combo summary/current value columns, disclaimer gating, and degraded fallback captioning.
- `tests/test_dashboard_positions.py` - regression coverage for combo valuation and truth helper behavior.
- `tests/test_options_order_lifecycle_dashboard.py` - dashboard source assertions for truth labels, disclaimer gating, and degraded captions.
- `tests/test_options_combo_dashboard_truth.py` - dedicated truth-source regression checks for combo dashboard behavior.

## Decisions Made
- Used explicit `truth_source` badges (`broker_combo`, `paper_strategy`, `ledger_rebuilt`) so operators can tell when dashboard rows are degraded.
- Let broker combo rows compute spread value from `combo_legs` metadata even when no ledger open position is available.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Guarded broker combo valuation when no ledger open position exists**
- **Found during:** Task 2
- **Issue:** broker combo pricing hit `NoneType.quantity` when the dashboard only had combo metadata and no ledger open position.
- **Fix:** added a safe quantity fallback before spread-aware valuation.
- **Files modified:** `core/dashboard_positions.py`
- **Verification:** `python3 -m pytest tests/test_dashboard_positions.py tests/test_options_order_lifecycle_dashboard.py tests/test_options_combo_dashboard_truth.py -v`
- **Committed in:** `e6358a9`

---

**Total deviations:** 1 auto-fixed (1 bug)
**Impact on plan:** Required for truthful broker combo valuation; no scope creep.

## Issues Encountered
- Targeted pytest runs still emit repository coverage warnings for narrowly-scoped suites, so full-suite verification remained the correctness gate.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness
- Wave 4 is complete: operator surfaces now expose truthful combo source and spread-aware values.
- Safe for Wave 5 verification work; remaining validation should focus on runtime/dashboard smoke coverage rather than more dashboard truth plumbing.

## Self-Check: PASSED

---
*Phase: 1000-implement-truthful-live-theta-execution-and-complex-order-or*
*Completed: 2026-04-21*
