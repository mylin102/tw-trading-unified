# tw-trading-unified

## What This Is

This is a Taiwan futures + options trading system built around Shioaji, with paper/live execution paths, runtime safety guards, and operator dashboards for session readiness and trade review. It is used to run and supervise real trading workflows where execution correctness, position truth, and recoverability matter more than feature breadth.

## Core Value

The system must preserve broker-truth execution state and capital safety so trading decisions and operator actions are based on correct, recoverable lifecycle data.

## Current Milestone: v1.2 Adaptive Strategy Optimization

**Goal:** Transform fixed-parameter spread strategies into volatility-adaptive systems to reduce friction and improve capture quality.

**Target features:**
- ATR-based dynamic thresholds for release stops and trailing exits in `tmf_spread`
- Volatility-gate entry filtering (Min ATR) to reduce transaction cost erosion
- Unified spread backtesting framework for cross-strategy efficiency comparison

## Requirements

### Validated

- ✓ Futures/options paper-live trading flows exist with session-aware monitors and operator dashboards — v1.0
- ✓ Core safety invariants are regression-locked, including duplicate-entry prevention, fee-inclusive PnL, session rollover, and paper capital guards — v1.0
- ✓ Shared bar-pipeline and readiness-state plumbing now align futures/options runtime and dashboard behavior more closely — v1.0
- ✓ ATR-based dynamic stop/trail logic implemented in `tmf_spread.py` — v1.2.1
- ✓ Min ATR entry filter implemented to reduce friction — v1.2.1
- ✓ Friction-aware backtest framework (`backtest_spread_v2.py`) validated 14.5% profit improvement and 60% cost reduction — v1.2.1

### Active

- [ ] Final production configuration for tomorrow's day session (Min ATR 10, Stop 2.0x, Trail 3.5x)
- [ ] Comprehensive unit tests for ATR-scaled logic in `tmf_spread.py`
- [ ] Integration verification: Ensure dashboard correctly reflects dynamic ATR thresholds
- [ ] Formalize one lifecycle state model for futures/options, paper/live execution (carried over from v1.1)
- [ ] Add restart-safe reconciliation so broker truth can be rebuilt after callback gaps or process restarts (carried over from v1.1)

### Out of Scope

- Rust Shioaji rewrite — deferred until the current Python execution path is boring and test-locked
- GCP migration — deferred until runtime supervision and execution correctness are stable locally
- Full dashboard redesign — only the lifecycle surfaces required to reflect execution truth belong in this milestone
- Vertical Spread optimization (Options) — deferred until Futures Spread reliability is proven

## Context

- The codebase already supports futures, options, and stock workflows, but the highest-risk runtime path is Taiwan futures/options execution.
- Recent backtests showed `tmf_spread` had a 52% friction ratio due to low-volatility noise trading.
- ADR-006 established the three-phase lifecycle (Entry-Release-Trail); v1.2 makes these phases volatility-adaptive.
- Using ATR to scale stops ensures the strategy "breathes" with the market, while Min ATR gates ensure entries only happen during tradable volatility.

## Constraints

- **Financial safety**: ATR-scaled stops must have hard minimums (5pt stop, 10pt trail) to prevent "stop-loss evaporation" in ultra-low vol.
- **Single source of truth**: Position and lifecycle state must derive from confirmed execution data.
- **Complexity management**: Keep ATR logic simple (multipliers) to avoid over-fitting during backtest.

## Key Decisions

| Decision | Rationale | Outcome |
|----------|-----------|---------|
| Use ATR for stop/trail scaling | Fixed points fail to distinguish between noise and trend in variable volatility | ✓ Implemented |
| Set Min ATR entry gate | High-frequency noise trading in low vol consumed >50% of gross profit in fees/tax | ✓ Implemented |
| Proxied ATR with spread_std in backtest | Validates logic using available historical spread volatility data | ✓ Good |
| Maintain hard floor for stops | Prevents logical errors or extremely tight stops that guarantee loss on slippage | ✓ Guarded |

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
