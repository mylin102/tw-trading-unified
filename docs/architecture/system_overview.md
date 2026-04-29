# System Overview

## Purpose

This system is an event-driven trading engine for Taiwan futures and options, designed to:

- Generate strategy signals from real-time market data
- Execute trades via structured instruments (e.g., vertical spreads)
- Maintain deterministic order lifecycle and state tracking
- Provide full observability of decision-making (RouterTrace)

It is not a research notebook or backtesting-only system; it is built for live or paper trading execution with production-like constraints.

---

## Runtime Components

The system is composed of four primary runtime modules:

- **FuturesMonitor**
  - Ingests tick data and builds canonical bars (5m)
  - Drives futures strategy evaluation

- **Strategy Router**
  - Evaluates all strategies per bar
  - Produces a single decision or "no trade"
  - Emits structured evaluation trace (RouterTrace)

- **OptionsMonitor**
  - Converts directional signals into structured option trades (e.g., vertical spreads)
  - Manages entry/exit lifecycle and PnL

- **IngestionService**
  - Handles API backfill and data warmup
  - Ensures system readiness after restart

These four modules run inside a single PM2-managed process (`trading-system`), sharing the same Shioaji API connection via `main.py` dispatchers. A second PM2 process (`stock-monitor`) handles equities watchlist scanning independently.

---

## Data Flow

The data pipeline follows a layered structure:

1. Raw tick ingestion → stored as CSV (P1)
2. Canonical bar reconstruction (5m preferred)
3. Indicator computation and enrichment
4. Strategy evaluation per bar

Canonical bar priority:

- tick-derived 5m (primary, zero API cost)
- API-derived 1m → rebuilt 5m (periodic backfill)
- legacy API 5m fallback (recovery only)

Strategy evaluation is a read-only consumer of canonical bars. No strategy code fetches raw API data.

See: [data_pipeline.md](./data_pipeline.md)

---

## Strategy Flow

Each 5-minute bar triggers a full strategy evaluation cycle:

1. All enabled strategies are evaluated independently
2. Each strategy produces a `StrategyEval`:
   - triggered / skip_reason / edge_score
3. Router selects one strategy (or none)
4. Decision is emitted as `RouterTrace`

Design principle:

- No trade is a valid outcome
- Every decision must be observable

See: [strategy_router.md](./strategy_router.md)

---

## Options Execution Flow

Directional signals are not executed as naked options.

Instead, all entries are converted into structured positions:

- CALL → Bull Call Spread
- PUT → Bear Put Spread

Execution steps:

1. Strategy router emits direction
2. OptionsMonitor invokes spread selector
3. Spread is validated (reward/risk >= 1.5, bid/ask < 0.30, friction check)
4. Position is opened and tracked

If the spread selector rejects (e.g., reward/risk too low), the system falls back to single-leg entry with a logged reason.

See: [option_vertical_spread.md](../strategy/option_vertical_spread.md)

---

## Observability

The system provides full decision traceability via:

- **RouterTrace (JSONL)**
  - One record per bar
  - Includes all strategy evaluations with skip reasons
  - Written to `logs/router_trace/router_trace_YYYYMMDD.jsonl`

- **StrategyEval**
  - Per-strategy trigger/skip reasons
  - Edge score and context notes (e.g., `NO_FIRE_EVENT`, `NO_COUNTER_EXTREME`)

- **Dashboard (Pipeline tab)**
  - Latest strategy status cards
  - Edge score timeline
  - Skip reason distribution chart
  - Raw trace viewer

- **Hourly audit log**
  - Summary of regime, trades, data health per hour

See: [no_trade_diagnosis.md](../operations/no_trade_diagnosis.md)

---

## Operational Boundaries

- System requires warmup (>= 20 bars) before trading is allowed
- Backfill from raw tick CSV is mandatory after restart to rebuild in-memory bars
- Tick ingestion depends on Shioaji contract subscription — contract must be resolved before ticks arrive
- Strategy execution is gated by data readiness (`is_trading_ready`)
- VPN instability causes `Connection reset by peer` on Shioaji socket → PM2 auto-restart; in-memory state lost until next 5m boundary
- Night session spread data is CSV-sourced (not live) and age-gated at 120 minutes

See: [pm2_debugging.md](../operations/pm2_debugging.md)

---

## Related Documents

Architecture:
- [data_pipeline.md](./data_pipeline.md)
- [strategy_router.md](./strategy_router.md)

Operations:
- [no_trade_diagnosis.md](../operations/no_trade_diagnosis.md)
- [pm2_debugging.md](../operations/pm2_debugging.md)

Decisions:
- [adr_001_disable_theta_gang.md](../decisions/adr_001_disable_theta_gang.md)
- [adr_002_vertical_spread_default.md](../decisions/adr_002_vertical_spread_default.md)

Strategy:
- [option_vertical_spread.md](../strategy/option_vertical_spread.md)
- [counter_vwap.md](../strategy/counter_vwap.md)
