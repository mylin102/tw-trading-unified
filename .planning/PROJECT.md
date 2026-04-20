# tw-trading-unified

## What This Is

This is a Taiwan futures + options trading system built around Shioaji, with paper/live execution paths, runtime safety guards, and operator dashboards for session readiness and trade review. It is used to run and supervise real trading workflows where execution correctness, position truth, and recoverability matter more than feature breadth.

## Core Value

The system must preserve broker-truth execution state and capital safety so trading decisions and operator actions are based on correct, recoverable lifecycle data.

## Current Milestone: v1.1 vNext Execution Reliability

**Goal:** Formalize the Shioaji order lifecycle into a reconcileable execution-truth model and lock it down with V-model validation.

**Target features:**
- Shared futures/options, paper/live lifecycle contract built around intent, order, and deal records
- Broker-truth reconciliation flow that combines callback events with `update_status()` recovery after callback gaps or restart
- Minimal dashboard alignment so entry/exit orders, fills, cost basis, and realized/unrealized PnL all reflect the same lifecycle truth

## Requirements

### Validated

- ✓ Futures/options paper-live trading flows exist with session-aware monitors and operator dashboards — v1.0
- ✓ Core safety invariants are regression-locked, including duplicate-entry prevention, fee-inclusive PnL, session rollover, and paper capital guards — v1.0
- ✓ Shared bar-pipeline and readiness-state plumbing now align futures/options runtime and dashboard behavior more closely — v1.0

### Active

- [ ] Formalize one lifecycle state model for futures/options, paper/live execution
- [ ] Add restart-safe reconciliation so broker truth can be rebuilt after callback gaps or process restarts
- [ ] Make dashboard lifecycle views reflect separate entry/exit orders, fills, costs, and PnL from the same source of truth
- [ ] Lock the redesign with V-model tests for partial fills, cancel/reject, trading-day mapping, and recovery behavior

### Out of Scope

- Rust Shioaji rewrite — deferred until the current Python execution path is boring and test-locked
- GCP migration — deferred until runtime supervision and execution correctness are stable locally
- Strategy redesign or signal tuning — this milestone is execution reliability hardening, not alpha work
- Full dashboard redesign — only the lifecycle surfaces required to reflect execution truth belong in this milestone

## Context

- The codebase already supports futures, options, and stock workflows, but the highest-risk runtime path is Taiwan futures/options execution.
- Recent debugging exposed lifecycle gaps: some trades showed fills without matching order history, entry/exit orders were collapsed together, night-session trading-day attribution drifted, and dashboard PnL/cost views were incomplete.
- `docs/shioaji_訂單生命週期.md` and `docs/example_order_manager.py` confirmed the needed direction: `place_order()` is not broker truth, order events and deal events are separate, and reconciliation via `update_status()` is mandatory.
- Recent reliability work already hardened shared bar handling, supervisor ownership, and false options squeeze-release exits; this milestone builds on that safer runtime base.

## Constraints

- **Financial safety**: Bugs can cause real loss — position, fills, and PnL must remain correct under live/paper execution.
- **Single source of truth**: Position and lifecycle state must derive from confirmed execution data, not UI assumptions or optimistic order placement.
- **Compatibility**: `main.py` and the 8500 dashboard must keep working while lifecycle internals are upgraded.
- **Broker model**: Shioaji callbacks are incomplete on their own — reconciliation and partial-fill handling must be first-class.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Keep `PaperTrader.position` as the position truth anchor | Trading invariants already rely on it; lifecycle work must strengthen, not bypass, that contract | — Pending |
| Model execution as intent → order → deal instead of flattening to one trade record | Shioaji exposes order and deal as separate truth sources and restart recovery depends on that separation | — Pending |
| Scope v1.1 to futures + options, paper + live, with minimal dashboard changes | This closes the execution correctness gap without destabilizing unrelated UI or infra work | — Pending |
| Defer Rust and GCP migration | Reliability must be proven in the current stack before platform changes add more variables | ✓ Good |
| Keep strategy semantics out of this milestone | The current problem is lifecycle truth and recoverability, not signal logic | ✓ Good |

## Evolution

This document evolves at phase transitions and milestone boundaries.

**After each phase transition**:
1. Requirements invalidated? -> Move to Out of Scope with reason
2. Requirements validated? -> Move to Validated with phase reference
3. New requirements emerged? -> Add to Active
4. Decisions to log? -> Add to Key Decisions
5. "What This Is" still accurate? -> Update if drifted

**After each milestone**:
1. Full review of all sections
2. Core Value check — still the right priority?
3. Audit Out of Scope — reasons still valid?
4. Update Context with current state

---
*Last updated: 2026-04-20 after milestone v1.1 initialization*
