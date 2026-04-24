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

This phase established the repo's main shape: `main.py` as the orchestrator, `strategies/futures/` and `strategies/options/` as the execution engines, and `ui/dashboard.py` as the operator cockpit.

### Phase 2 — Strategy expansion and V-model hardening (2026-04-03 to 2026-04-08)

The next wave expanded the repo from a simple unified runner into a **multi-strategy trading platform**.

Key themes:

- strategy plugins
- ThetaGang integration
- QuantLib pricing support
- institutional backtest dashboard work
- elite strategy curation
- stock integration and multi-asset scanning
- repeated V-model / SDD-driven design and test waves

This is also the period where documentation volume exploded: each strategy wave, dashboard redesign, and architecture shift generated its own design notes and review files.

### Phase 3 — Stock/CANSLIM and adaptive-system growth (2026-04-10 to 2026-04-17)

The repo then broadened from a futures/options engine into a larger **research + execution platform**.

Key themes:

- CANSLIM pattern engine and stock watchlist automation
- adaptive strategy framework
- hourly audit timeline
- order lifecycle tracking
- decision intelligence / edge filter upgrades
- more operational and deployment guidance

By this point, the repository contained not only production code but also:

- experiment notes
- migration reports
- readiness reviews
- post-session reviews
- branch-scoped docs

This explains why the repo now has both enduring docs and many point-in-time reports mixed together.

### Phase 4 — Runtime stabilization and incident-driven infrastructure work (2026-04-13 to 2026-04-20)

The most important recent phase was **infrastructure stabilization**.

This phase focused less on new strategy ideas and more on making the runtime trustworthy:

- unified session logic
- stale-contract handling
- config path alignment between dashboard and monitors
- options position/accounting fixes
- order lifecycle export and dashboard visibility
- stale-tick watchdogs
- shared data pipeline work
- documentation consolidation wave
- supervisor conflict cleanup
- shared bar pipeline hardening

This phase is the foundation for the next milestone. It reduced the chance that "strategy problems" are actually caused by stale data, duplicate supervisors, or broken logging contracts.

## The major infrastructure milestones

### 1. Unified runtime ownership

One of the deepest operational problems was conflicting ownership of the trading core:

- `PM2` resurrected `main.py`
- `autostart.sh` also killed / launched / health-checked `main.py`
- `main.py` enforced a single-instance PID lock

That created false crash loops and dangerous cooldown behavior during trading hours.

The repo now converged on a cleaner contract:

- **PM2 owns `main.py`**
- **autostart owns dashboards / maintenance only**
- **PID lock remains the final duplicate guard**

This is a major historical turning point because it separates process supervision from trading logic and removes one whole class of recovery failures.

### 2. Shared futures/options bar pipeline

Another major turning point was the move to a **shared canonical bar contract**.

Before the cleanup, futures and options built their 5m/15m/1h bars through different paths and fallback rules. That caused:

- session-boundary mismatches
- stale-bar gating errors
- empty or partial indicator rows
- hard-to-debug dashboard inconsistencies

The new shared pipeline standardized:

- canonical OHLCV handling
- session / trading-day labeling
- gap validation
- 1m to 5m promotion
- source selection diagnostics

This change matters historically because it turns the repo from "many ad hoc data paths" into a system with a real infrastructure layer.

### 3. Options data-chain repair

The recent options `score=0.0 / side空 / mid_trend空` issue was not a pure strategy bug. It exposed a deeper infrastructure bug:

- 1-minute MTX history was being treated as 5-minute bars
- validators rejected it
- signal generation fell back to early-return snapshots

The repair aligned options with the shared bar contract and restored proper signal maturity.

This is the clearest example of why the current milestone matters: the repo can now separate **data-chain failures** from **actual strategy quality**.

### 4. Hourly audit + self-repair

The repo also evolved from passive logging to more active operational defense.

The hourly no-trade audit now serves as part of the historical progression toward a more autonomous runtime:

- first it logged "why no trade"
- now it can also inspect options bar / indicator health
- and trigger safe repair steps when data goes missing

That is a meaningful shift from retrospective debugging to runtime resilience.

## Why there are so many documents

The documentation sprawl comes from four different document classes being mixed together:

1. **Canonical docs**
   - enduring operator/developer references
   - examples: `README.md`, `RULES.md`, `docs/architecture.md`, `docs/operations.md`

2. **Historical reports**
   - readiness reports, reviews, migration notes, incident writeups
   - these should mostly live in `docs/archive/`

3. **Design snapshots**
   - SDD, V-model, strategy plans, architecture proposals
   - useful as history, but often not canonical after implementation lands

4. **Tooling/agent notes**
   - model-specific or workflow-specific files
   - often useful temporarily, but not always part of long-term project docs

The repo is not "over-documented" by accident; it is a sign that the project was moving through many stabilization and review loops quickly. The cleanup job is to **separate enduring reference from historical evidence**, not to erase the history.

## Recommended way to read the repo now

If someone wants to understand the repo in order:

1. `README.md` — entry point
2. `RULES.md` — trading safety invariants
3. `docs/architecture.md` — current structure
4. `docs/operations.md` — runtime / operational behavior
5. `docs/strategies.md` — strategy surface
6. `docs/repo-history.md` — this file, for the timeline and context
7. `docs/archive/` — only for historical incidents, readiness, migration, and reviews

## What this history means for the next milestone

The repo is now at a transition point.

Earlier work was dominated by:

- unification
- dashboard assembly
- multi-strategy expansion
- runtime stabilization
- data-pipeline correctness

The next milestone can spend more time on:

- strategy quality
- signal precision
- trade review and attribution
- post-trade learning loops

That shift is important: as infrastructure gets more deterministic, trading results become more trustworthy as feedback.

## Documentation cleanup implication

This historical map suggests a medium-strength cleanup rule:

- keep a **small canonical set** at the repo root and in `docs/`
- keep **historical evidence** under `docs/archive/`
- avoid duplicate root-vs-`docs/` copies
- prefer one consolidated history document over many loosely overlapping status reports

This file should be used as the anchor for the next documentation cleanup wave.

## 2026-04-24 — Position sizing reduction (config tuning)

**Context**: After TMF → MXF migration (MXF = 微台指, 10 TWD/pt vs 50 TWD/pt), options and futures position sizing was too aggressive relative to the smaller contract unit. Two config changes reduce per-trade exposure.

**Changes**:
- `config/futures.yaml`: `lots_per_trade: 2 → 1` — futures now enters 1 lot per signal instead of 2
- `config/options_strategy.yaml`:
  - `risk_mgmt.lots_per_trade: 2 → 1` — options enters 1 lot per signal instead of 2
  - `risk_mgmt.max_positions: 15 → 12` — reduces max concurrent options positions
  - Trailing zero cleanup on `max_spread_pct: 0.10 → 0.1` and `max_iv: 0.40 → 0.4`

**Rationale**: These are conservative sizing adjustments while the system stabilizes post-migration. Lower lots_per_trade reduces paper loss exposure per signal. Fewer concurrent positions limits portfolio-wide risk during the learning phase. MXF's smaller point value already reduces absolute risk per lot, but keeping lots=2 on top of that was unnecessary.
