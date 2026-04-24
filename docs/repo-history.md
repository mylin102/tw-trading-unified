# Repository History

This document consolidates the historical context of the `tw-trading-unified` repository so future cleanup, strategy reviews, and infrastructure work can start from one timeline instead of many scattered reports.

## Current repository identity

The repository is a unified Taiwan trading system centered on one runtime:

- **Futures** execution and risk control
- **Options** execution, Greeks, and strategy monitoring
- **Stocks** scanning, watchlists, and strategy research
- **Streamlit dashboards** for live monitoring and backtesting
- **Operational safety infrastructure** around Shioaji sessions, paper/live boundaries, logs, and restart behavior

The codebase moved quickly during April 2026, so the current documentation sprawl is mostly a byproduct of many overlapping stabilization waves, incident reviews, and branch-specific reports.

## High-level evolution

### Phase 1 — Unification and dashboard foundation (2026-04-01 to 2026-04-02)

The repo started its current form by unifying futures and options under a **single Shioaji session** and a shared control surface.

Key themes:

- unified trading repo
- live/paper safety controls
- one Streamlit dashboard on port 8500
- shared capital controls and monitoring tabs
- early options logging improvements

### Phase 2 — Options stabilization and Logfire removal (2026-04-03 to 2026-04-06)

Focus shifted to options-specific features and removing Logfire.

Key themes:

- live and paper mode controls hardened
- options system stabilization
- Logfire dependency removal
- Shioaji thread safety improvements
- Textual dashboard exploration (short-lived, port 8501)

### Phase 3 — Stock system + structural refactors (2026-04-07 to 2026-04-13)

Stock monitoring and several structural improvements.

Key themes:

- stock system integration from squeeze-backtest
- `stop()` method implementation and process safety
- order management refactoring
- futures trailing stop / auto-exit
- options gap between live and paper modes
- stock price display on dashboard
- test infrastructure added

### Phase 4 — DX improvements and testing (2026-04-14 to 2026-04-16)

Developer experience and testing infrastructure.

Key themes:

- DX and documentation improvements
- test speed optimizations
- unused code removal
- Mac auto-startup
- futures/options process monitoring for PM2

### Phase 5 — PM2 stability and data consistency (2026-04-17 to 2026-04-18)

Persistence and process monitoring.

Key themes:

- PM2 process monitoring
- data consistency fixes
- PM2 restart reliability
- watchdog role clarity
- run_trading.sh improvements
- position recovery on restart
- Shioaji rolling contract handling

### Phase 6 — Position consistency and error recovery (2026-04-19 to 2026-04-22)

Trading session reliability and position audits.

Key themes:

- position / trade / option record consistency
- night session monitoring
- overnight position handling
- session transition cleanup
- attribution and decision logging

### Phase 7 — MXF migration and position sizing (2026-04-23 to 2026-04-24)

Migration from TMF (50 TWD/pt) to MXF (10 TWD/pt), and config tuning.

Key themes:

- TMF → MXF contract migration
- Calendar Condor v2.0 integration
- position sizing reduction
- entry score tuning
- debug code cleanup

## Authoritative source

The following should be preferred as the source of truth:

- for real-time status of the trading system → PM2 logs / Streamlit dashboard
- for historical trading decisions and outcomes → the continuous log of `git log --oneline`
- for operational configuration → `config/futures.yaml`, `config/options_strategy.yaml`, `config/stocks.yaml`
- for strategy plugins and scoring → the strategy Python files in `strategies/futures/` and `strategies/options/`
- for roadmap and incident tracking → consolidated reports under `docs/`
- for cross-repo contract between tw-trading-unified and squeeze-backtest → `docs/contracts/`
- for Shioaji-specific behaviors and Taiwan market rules → `docs/taiwan-market-rules.md`
- for the unified picture of what changed, why it changed, and when → this file

### What this file is NOT authoritative for

- the exact configuration of a specific backtest run (YAML in squeeze-backtest)
- the day-to-day portfolio decisions inside squeeze-backtest
- low-level timestamps of every data fetch, API call, or log event
- stock screening algorithms (those live in squeeze-backtest)
- position-level P&L in a running session (Streamlit or trade CSV files are better)

### When to update this file

Update this file when:

- a new strategy is added or removed
- a system-wide config change (e.g., paper/live boundary, Shioaji session model, risk limits)
- a significant bug or incident and its resolution
- a major infrastructure change (e.g., PM2, dashboard, data pipeline)
- a key contract boundary changes between tw-trading-unified and squeeze-backtest

Do NOT update this file for:

- individual backtest parameter changes
- routine data collection or rebalancing of the watchlist
- minor log output changes
- comments or docstring improvements

Still, use judgment and prioritization — this file is meant to be a readable historical summary, not an exhaustive log.

### Recommended structure for timeline entries

```
## YYYY-MM-DD — Short title
**Context**: ... (why this happened)
**Changes**:
- file/path: detail
**Rationale**: ... (why this way)
```

## 2026-04-01 — Repository unification and dashboard setup

**Context**: First Wave. Unifying futures and options repos under a single Shioaji session and dashboards. All work on 2026-04-01 is initial setup.

**Changes**:

- unified trading repo
- live/paper safety controls
- one Streamlit dashboard on port 8500
- shared capital controls and monitoring tabs
- early options logging improvements

No stable version at end of day. More of a scaffold than a production system.

## 2026-04-02 — Live trading stabilization

**Context**: Second Wave. After the unified repo was set up, the focus shifted to making it work in live paper mode on the second day.

**Changes**:

- futures auto exit
- news sentiment
- live/paper mode fix
- options session refactoring
- risk/reward monitoring

## 2026-04-03 — Options stabilization and thread safety

**Context**: Focus on options running reliably in paper mode with Shioaji.

**Changes**:

- options session management
- live mode and paper mode fixes
- Shioaji API initialization safety
- account config recovery

## 2026-04-04 — Logfire removal and error recovery

**Context**: Remove Logfire dependency — it was a third-party logging integration that introduced complexity without commensurate value. The dependency was adding startup failures and configuration overhead.

**Changes**:

- Logfire removed from requirements
- `logfire` calls removed from futures monitor, option monitor, dashboard, and common modules
- error recovery for paper/live mode mismatch after broker restarts
- graceful handling of config account data recovery

## 2026-04-05 — Options integration and data quality

**Context**: Focus on options pricing data quality and single-option leg management.

**Changes**:

- `live_options_squeeze_monitor.py` — new options monitor integration
- options data pipeline
- single-leg option trading on TMF
- paper account verification
- options config from external YAML
- dashboard v2 transition

## 2026-04-06 — Dashboard and UX improvements

**Context**: Better Streamlit dashboard with trading controls and status displays.

**Changes**:

- dashboard progress and status display
- trading controls introduced
- auto-focus on password field
- futures and options refactoring
- early Textual dashboard exploration (short-lived, port 8501)

## 2026-04-07 — Stock monitoring and system control

**Context**: Adding stock monitoring wall, better process management, and position recovery.

**Changes**:

- stock monitoring system
- unified process lifecycle management
- position recovery on broker reconnect
- Shioaji API key management
- futures strategy alignment (trailing stop, auto-exit)
- position file recovery improvements
- contract specification fixes for futures
- report on data consistency improvements (YAML-based)
- stock price display on dashboard with delisted price handling
- tw_stock_monitor — integration of industry rotation and sentiment
- stock focus list extracted from squeeze-backtest for data collection
- significant data pipeline work for squeeze related data structures
- embedded kanban and documentation in the dashboard

## 2026-04-08 — Cross-system data quality

**Context**: Data correctness between stock scanning, futures, and options systems.

**Changes**:

- stock monitoring improvements
- contract specification adjustments
- data gap between squeeze-backtest and trading system
- dashboard improvements (real-time stock updates)
- industry rotation and institutional data for stocks
- Calendar Spread monitoring (futures)
- more robust data consistency improvements including sentiment data and configuration

## 2026-04-09 — Unused code removal

**Context**: System stabilization and general codebase improvement across modules.

**Changes**:

- refactoring of session management for stability
- data quality improvements in stock data pipeline
- bear defense and momentum filters for futures
- unused code removal
- unit tests and test infrastructure
- dashboard indicators and monitoring improvements
- options trade management improvements
- stock focus list updates

## 2026-04-10 — Strategy review and financial data

**Context**: Financial statement data extraction and consolidated review of all trading strategies.

**Changes**:

- financial data extraction from API for earnings and institutional holdings
- consolidated strategy review across futures, options, and squeeze
- bear defense and regime filters
- Dashboard monitoring

## 2026-04-11 — Test infrastructure

**Context**: Refactoring test infrastructure and documentation.

**Changes**:

- test infrastructure (mocked Shioaji fixtures, data-driven and framework tests, `numpy` fixture for test speed)
- docs: `docs/testing-guide.md`
- docs: `docs/repo-history.md` (initial version)
- docs: `docs/taiwan-market-rules.md`
- docs: `docs/contracts/README.md` + cross-repo contracts
- docs: `docs/glossary.md`

## 2026-04-12 — Night session handling

**Context**: Test infrastructure, session management improvements, night-market handling of TMF data. Enhancing data availability and position accuracy across sessions.

**Changes**:

- night session handling improvements
- gap detection and data completion
- quarterly roll handling for futures
- PM2 process monitoring started this day
- test infrastructure improvements and more robust triggers
- enhanced position recovery with session awareness
- optimized data processing for session transitions
- night session performance improvements
- position summary fixes and position file improvements

## 2026-04-13 — Strategy review and PM2

**Context**: Second strategy review and PM2 stability improvements.

**Changes**:

- PM2 process stability improvements
- order lifecycle management improvements
- data pipeline improvements (tick-to-bar for futures)
- stock system watchlist and CANSLIM integration
- strategy review processes and documentation (consolidation report and attribution)
- futures strategy documentation and blackboard-style monitoring

## 2026-04-14 — DX, testing, and contract resolution

**Context**: Developer experience improvements, test speed, and Shioaji contract resolution.

**Changes**:

- Shioaji contract resolution (rolling contracts for TMF, MXF, TXO)
- test performance optimization
- unused code removal
- trailing stop for futures
- docs: `docs/dx-improvements.md`

## 2026-04-15 — Mac autostart and startup investigation

**Context**: Improve system resilience to unexpected shutdowns.

**Changes**:

- mac autostart for the two PM2 processes
- startup investigation methodology (skill creation)
- test-only file refactoring: `tests/test_common.py` → `tests/test_shioaji_contract_resolver.py` etc.

## 2026-04-16 — Audit, data, and session transitions

**Context**: Position audit mechanism, data consistency upgrades, and session transition handling.

**Changes**:

- position audit tool: `scripts/audit_positions.py`
- data consistency improvements across market data and OHLCV pipeline
- attribution logic improvements
- session transition improvements for overnight positions and gap filling
- PM2 monitoring improvements for stable startup

## 2026-04-17 — PM2 monitoring and data repair

**Context**: PM2 monitoring improvements, data consistency upgrades, and data repair/backfilling.

**Changes**:

- PM2 monitoring improvements (watchdog role)
- data consistency packages across entire data pipeline (gap filling)
- data repair and backfilling scripts
- order management safety and reliability
- dashboard reliability (robust session handling)
- test framework reliability (random seed, conftest cleanup)
- plan to deprecate old-style attribution references

## 2026-04-18 — Attribution, order management, and stability

**Context**: Attribution logging, order management refactoring, and PM2 restart reliability.

**Changes**:

- unified attribution logging system
- order management architecture improvements
- PM2 stability improvements (restart reliability)
- dashboard stability improvements

## 2026-04-19 — Position consistency and session resilience

**Context**: Position consistency between audit and paper systems, session transition handling.

**Changes**:

- position record consistency across modules
- options position record mapping
- session transition order cleanup
- night session monitoring
- order lifecycle tracking and trade summary
- data consistency across sessions

## 2026-04-20 — Futures strategy and process monitoring

**Context**: Futures strategy performance review, trade parameter review, and process monitoring for stable overnight operation.

**Changes**:

- futures strategy parameters and performance
- trade parameter improvements
- process monitoring implementation
- night session data monitoring
- performance metrics and attribution
- monitoring automation and summary generation

## 2026-04-21 — Options optimization and PM2 migration

**Context**: Options optimization, PM2 migration, and strategic performance reviews.

**Changes**:

- options strategy optimization
- PM2 migration from terminal to PM2
- calendar spread and stock attribution improvements
- strategic futures/options performance review
- test structure improvements

## 2026-04-22 — Calendar Condor v2.0 and strategy routing

**Context**: Major Calendar Condor overhaul to solve Shioaji rolling contract issues, and fix options priority routing between ThetaGang and directional strategies.

**Changes**:

- Calendar Condor v2.0 with ContractResolver for Shioaji rolling contracts
- fixed options strategy priority: directional (trend) now takes priority over ThetaGang (range-bound) when signal conditions are met
- completed strategic review of squeeze-backtest CANSLIM integration plan
- nightly session monitoring and attribution
- empirical performance regression analysis of Calendar Condor v2.0

**Rationale**: The original architecture prioritized ThetaGang over directional trading. For a market trending as strongly as Taiwan's regular session, directional strategies should execute first. ThetaGang is better suited as a fallback when no directional signal is active.

## 2026-04-23 — TMF → MXF migration

**Context**: Migrated from TMF (大型台指期, 50 TWD/pt) to MXF (微型台指期, 10 TWD/pt) to reduce per-lot risk and improve position granularity. This is a strategic contract migration affecting all futures and options strategies.

**Changes**:

- TMF references replaced with MXF across all strategy logic, configs, and data pipelines
- Contract resolution updated for MXF rolling contracts
- Options underlying updated from TMF to MXF pricing
- Position sizing recalculated for smaller point value
- Calendar Condor v2.0 ported to MXF (backtest: 21 days, +2,986 TWD, 2.99% return, 94.9% win rate)
- Squeeze backtest signals updated for MXF

**Rationale**: TMF's 50 TWD/pt made it difficult to size positions appropriately for the 40,000 TWD paper capital limit. MXF at 10 TWD/pt provides finer granularity, better risk control, and allows the system to enter positions more freely while respecting the capital constraint.

## 2026-04-24 — Position sizing reduction (config tuning)

**Context**: After TMF → MXF migration (MXF = 微台指, 10 TWD/pt vs 50 TWD/pt), options and futures position sizing was too aggressive relative to the smaller contract unit. Two config changes reduce per-trade exposure.

**Changes**:
- `config/futures.yaml`: `lots_per_trade: 2 → 1` — futures now enters 1 lot per signal instead of 2
- `config/options_strategy.yaml`:
  - `risk_mgmt.lots_per_trade: 2 → 1` — options enters 1 lot per signal instead of 2
  - `risk_mgmt.max_positions: 15 → 12` — reduces max concurrent options positions
  - Trailing zero cleanup on `max_spread_pct: 0.10 → 0.1` and `max_iv: 0.40 → 0.4`

**Rationale**: These are conservative sizing adjustments while the system stabilizes post-migration. Lower lots_per_trade reduces paper loss exposure per signal. Fewer concurrent positions limits portfolio-wide risk during the learning phase. MXF's smaller point value already reduces absolute risk per lot, but keeping lots=2 on top of that was unnecessary.

## 2026-04-24 — Config refinement & debug cleanup (post-MXF tuning)

**Context**: Continued tuning after MXF migration — futures entry_score lowered, options entry_score raised, stocks reorganized, and debug code from NoneType investigation cleaned up.

**Key observations**:
- The `NoneType has no len()` error that plagued earlier sessions was **NOT caught by the main loop try/except** (line 2327 in monitor.py), despite matching the error format string. This was conclusively proven by adding traceback file-write code inside that except — the file was never created, meaning a different handler (or a stale .pyc) was responsible.
- After `PYTHONDONTWRITEBYTECODE=1` restart + config tuning, the error **disappeared entirely** — system running cleanly with 771 bars loaded, 0 signals generated.
- Futures generating ⚠️ NO_VALID_SIGNALS (771 bars, 0 signals) — strategy entry_score=13 may still be too strict.
- Options: directional signal active (C score=93.3) but release confirmation still pending.

**Changes**:
- `config/futures.yaml`:
  - `strategy.entry_score: 16 → 13` — lower entry threshold to generate more signals
  - `trade_mgmt.max_positions: 10 → 5` — tighter cap during stabilization
- `config/options_strategy.yaml`:
  - `strategy.entry_score: 10 → 15` — raised to reduce false signals
  - `risk_mgmt.max_positions: 12 → 10` — fewer concurrent positions
- `config/stocks.yaml`:
  - Reorganized: moved strategy params (atr_mult, bear_defense, capital_per_trade, etc.) to the end of the file for cleaner watchlist-first layout
  - `capital_per_trade: 20000 → 50000` — increased per-trade budget
- `strategies/futures/monitor.py`:
  - Removed debug traceback code (fm_tb.txt, format_exception print) from the main loop except block — this was temporary diagnostic instrumentation from the NoneType investigation and is no longer needed

**Rationale**: Futures needed lower entry threshold after MXF migration (smaller per-tick value means scores are naturally lower). Options raised entry score to filter noise. Stocks config reorganized for maintainability. Monitor.py cleanup restores production-ready state.
