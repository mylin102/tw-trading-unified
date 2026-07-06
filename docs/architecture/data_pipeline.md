# Data Pipeline

## Overview

The data pipeline is designed as a layered system to ensure deterministic, recoverable, and observable market data flow.

It separates:

- Raw ingestion (tick-level truth)
- Canonical bar construction (strategy input)
- Recovery and health monitoring

This design prevents strategies from directly depending on unreliable or partial data sources.

---

## Architecture

The pipeline consists of three layers:

- **P1: Raw Tick Layer** — capture all incoming ticks as the single source of truth
- **P2: Canonical Bar Layer** — provide consistent bar representation for all strategies
- **P3: Recovery & Monitoring Layer** — ensure system recovery and data health

---

## P1 — Raw Tick Layer

### Purpose

Capture all incoming ticks as the single source of truth.

### Responsibilities

- Subscribe to Shioaji exchange feed (futures + options)
- Append tick data to in-memory deque (for real-time) and CSV storage (for persistence)

### Output

- deque (live ticks for immediate bar building)
- `logs/raw_ticks/{contract}_{YYYYMMDD}_ticks.csv`

### Design Principles

- Write-before-use: CSV is written before any in-memory state change
- Append-only: ticks are never modified after storage
- Lossless capture: every tick callback is stored

---

## P2 — Canonical Bar Layer

### Purpose

Provide a consistent 5-minute bar representation for all strategies.

### Source Priority

1. Tick-derived 5m bars (zero API cost, real-time)
2. API 1m → resampled to 5m (periodic backfill, rate-limited to 120s)
3. Legacy API 5m CSV (recovery only, never triggered from strategy tick)

### Responsibilities

- Build bars from tick deque (`_tick_bars_deque`)
- Rebuild bars from API via `IngestionService._periodic_backfill_bars()`
- Maintain `df_5m` for indicator computation

### Constraint

`len(df_5m) >= 20` — otherwise `trading_ready = False`, no strategy execution.

### Indicator Computation

Each canonical bar runs `calculate_futures_squeeze()` which produces squeeze, momentum, regime, and breakout signals. The resulting enriched DataFrame is the sole input to strategy evaluation.

---

## P3 — Recovery & Monitoring Layer

### Purpose

Ensure system recovery after restart and monitor data health during operation.

### Responsibilities

- API backfill on startup: rebuild in-memory bars from raw tick CSV
- Detect stale feeds via freshness SLA checks (warn at 120s, critical at 600s)
- Gate trading until readiness conditions are met

### Backfill Strategy

```
If raw tick CSV exists for today → rebuild bars from ticks
Else → fallback to API kbars() for recent history
```

### Readiness Conditions

All must be true:
- `df_5m` is not None and not empty
- `len(df_5m) >= 20`
- Latest bar timestamp is within `STALE_CRITICAL_SECS` of current time

---

## Data Flow

```
Exchange Tick Feed (Shioaji)
       │
       ▼
┌─────────────────────┐
│  P1: Raw Tick Layer  │
│  ┌───────────────┐   │
│  │ in-memory deque│   │
│  └───────┬───────┘   │
│          │            │
│  ┌───────▼───────┐   │
│  │ RawTickWriter  │   │
│  │ (CSV)         │   │
│  └───────────────┘   │
└──────────┬──────────┘
           │
           ▼
┌─────────────────────────┐
│ P2: Canonical Bar Layer │
│ Build 5m bars from deque│
│ Resample API 1m if gaps │
│ Compute indicators       │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│     Strategy Router      │
│ (reads df_5m only)       │
└─────────────────────────┘
```

---

## Failure Modes

| Failure | Symptom | Recovery |
|---|---|---|
| No tick ingestion | feed_health shows stale ages | Check Shioaji subscription, VPN |
| Warmup stuck | `is_trading_ready = False`, no bars | API backfill or tick CSV rebuild |
| Stale bar | Canonical freshness SLA breached | P2 backfill catches up at next cycle |
| Duplicate bars | Overlapping timestamps in deque | `drop_duplicates(subset='timestamp')` on concat |

---

## Design Principles

- **Single Source of Truth:** Raw tick CSV is the authoritative record. Everything else is derived.
- **Layer Isolation:** P1 never reads from P2. P2 never calls P3. P3 never calls the API from strategy_tick.
- **Deterministic Reconstruction:** Given the same raw tick CSV, the same bars can be rebuilt. No API calls needed.
- **Fail Closed:** If bars are stale or missing, no trades are executed until data recovers.

---

## Related Documents

- `docs/architecture/system_overview.md` — runtime components
- `docs/architecture/strategy_router.md` — consumes canonical bars
- `docs/operations/no_trade_diagnosis.md` — stale data debugging
- `strategies/futures/monitor.py` — FuturesMonitor implementation
- `strategies/futures/squeeze_futures/data/tick_writer.py` — RawTickWriter
