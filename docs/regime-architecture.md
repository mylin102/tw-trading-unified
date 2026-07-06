# Market Regime Architecture

**Last updated**: 2026-06-28

## Overview

The trading system has three **independent** regime layers, each serving a different
purpose and operating on a different timescale. They should NOT be conflated.

```
                ┌──────────────────────────────┐
                │     Market Regime Engine      │  ← NEW (Phase 1)
                │   (session-level, shared)     │
                │   data/market_regime.json     │
                └──────────┬───────────────────┘
                           │ consumed by
              ┌────────────┼────────────┐
              ▼            ▼            ▼
       Stocks Gate   Stocks Monitor  (Futures optional)
```

```
         Bar-level                    Options-specific
    ┌──────────────────┐         ┌──────────────────────┐
    │ FuturesBarRegime │         │   SurfaceEngine      │
    │ (per-bar, live)  │         │  SkewRegimeLogger    │
    │ TREND/SQUEEZE/.. │         │  VolStateMachine     │
    └──────┬───────────┘         │  (vol_regime:        │
           │ consumed by         │   EXPANDING/         │
           ▼                     │   COMPRESSING/       │
    FuturesStrategyRouter         │   NEUTRAL)           │
    → strategy plugins           └──────────┬────────────┘
                                            │ consumed by
                                            ▼
                                     Options strategies
                                     (theta_gang, etc.)
```

## Why Three Layers

| Layer | Timescale | Question Answered | Label Set |
|---|---|---|---|
| **Market Regime** | Session (hours) | "What kind of market are we in right now?" | BULL / STRONG / CHOP / WEAK / BEAR |
| **Futures Bar Regime** | Bar (5 min) | "Should this bar trigger routing?" | TREND / SQUEEZE / WEAK / BEAR / CHOP / STRETCHED |
| **Options Vol Regime** | Tick/Bar | "Is volatility expanding or compressing?" | EXPANDING / COMPRESSING / NEUTRAL |

They are complements, not substitutes. `FuturesBarRegimeResult` already has a
`session_regime` field to accept the market-level regime as context.

## Market Regime Engine (Phase 1)

### Purpose

Provide one authoritative session-level market state for strategies that do NOT
have their own regime classifier — specifically **stocks gate + stocks monitor**.

Futures and options continue to use their own regime pipelines.

### Core Principles

#### P1 — Single Writer Rule

Only `core/regime_engine.py` is allowed to compute and write
`data/market_regime.json`. All other modules are read-only consumers.
No strategy, monitor, or dashboard may independently compute market regime.

#### P2 — Read State, Not Log

Regime Engine reads dedicated state files, not audit logs.
`logs/skew_regime/*.jsonl` is for post-session audit.
`data/skew_state.json` is for real-time consumption by the engine.

#### P3 — Weight in Config

Feature weights live in `config/market_regime.yaml`, not in Python code.
Adding a new input source does not require code changes.

#### P4 — Hysteresis in File

Previous regime is stored in the output JSON (`previous_regime`), not in memory.
Process restart does not lose regime history.

#### P5 — No Self-Compute in Consumers

`monitor.py` and all consumers are prohibited from independently
computing market regime. They call `get_current_regime()` only.

### Data Flow

```
   surface_engine computes skew
         │
         ├──→ logs/skew_regime/*.jsonl      (audit — unchanged)
         └──→ data/skew_state.json           (state — NEW)
                                               │
   config/market_regime.yaml ─┐               │
                              ├──→ regime_engine.update()
   TAIEX 1d bars ─────────────┘               │
   (data/taifex_raw/)                          │
                                               ▼
                                        data/market_regime.json
                                               │
                                     ┌─────────┴─────────┐
                                     ▼                   ▼
                              market_gate.py       monitor.py
                                                   (replaces self-compute)
```

### Inputs

| Input | Weight | Source | Availability |
|---|---|---|---|
| Futures skew (z-score, directional) | 60% | `logs/skew_regime/YYYYMMDD.jsonl` last entry | Always available when options engine is running |
| TAIEX index trend (MA20/MA60 cross, EMA slope) | 40% | `data/taifex_raw/` or live kbars | Always available |

### Output

File: `data/market_regime.json`

```json
{
  "schema_version": 1,
  "generated_at": "2026-06-28T10:30:00+08:00",
  "expires_at": "2026-06-28T10:35:00+08:00",
  "age_ms": 0,
  "previous_regime": "STRONG",
  "regime": "CHOP",
  "score": 10,
  "confidence": 0.72,
  "transition_count": 2,
  "inputs": {
    "futures": {
      "available": true,
      "fresh": true,
      "score": -15
    },
    "index": {
      "available": true,
      "fresh": true,
      "score": 5
    }
  },
  "features": {
    "futures_skew": -15,
    "index_trend_ma20": 5,
    "index_trend_ma60": -8,
    "index_ema_slope": -2
  },
  "degraded": false,
  "degraded_reason": null
}
```

### Regime Map

| Score Range | Label | Gate Behaviour |
|---|---|---|
| >= 65 | BULL | ALLOW_LONG (full size) |
| 35 ~ 64 | STRONG | ALLOW_LONG (high-conviction filter) |
| -20 ~ 34 | CHOP | ALLOW_LONG (reduce size) |
| -55 ~ -21 | WEAK | ALLOW_DEFENSIVE_ONLY (scout only) |
| < -55 | BEAR | BLOCK_LONG |
| engine not running | UNKNOWN | BLOCK_LONG (fail-closed) |

### Hysteresis

Prevents rapid regime flipping when score oscillates around a threshold.

```
Transition            Uphill threshold    Downhill threshold
CHOP → STRONG         score >= 40         (N/A)
STRONG → CHOP         (N/A)               score <= 25
CHOP → WEAK           score <= -25        (N/A)
WEAK → CHOP           (N/A)               score >= -15
WEAK → BEAR           score <= -60        (N/A)
BEAR → WEAK           (N/A)               score >= -50
```

### Modules

| Module | Type | Responsibility |
|---|---|---|
| `core/regime_engine.py` | Writer | collect → compute → write |
| `core/regime_consumer.py` | Reader | read_market_regime() / get_current_regime() |
| `strategies/stocks/market_gate.py` | Consumer | reads market_regime.json (sole source). No fallback to skew_signal.json. |
| `strategies/stocks/monitor.py` | Consumer | replaces TAIEX self-computation with get_current_regime() |

### Safety

- **Fail-closed**: if `market_regime.json` is missing, stale, unreadable, or invalid,
  `market_gate.py` returns BLOCK_LONG.
  No fallback to skew_signal.json. skew_signal.json is an engine input, not a gate source.
- **Hysteresis**: prevents thrashing between adjacent regimes
- **No runtime dependency**: engine can run as a background task; consumer never blocks

## Migration Path

### Step 1 — Add writer + reader (no existing code changed)
Files: `core/regime_engine.py`, `core/regime_consumer.py`

### Step 2 — Switch market_gate.py to prefer market_regime.json
Gate checks two paths in order: `[market_regime.json, skew_signal.json]`.

### Step 3 — Replace monitor.py TAIEX self-computation
`_update_market_regime()` becomes a thin wrapper around
`regime_consumer.get_current_regime()`.

### Step 4 — (Optional) Wire periodic update into main.py
Call `regime_engine.update()` every N iterations.

## Comparison: Before vs After

### Before (current)
```
Market State is fragmented:
  - market_gate.py: only reads skew_signal.json (futures → stocks)
  - monitor.py: computes TAIEX EMA independently (no output)
  - No shared file for dashboard or other consumers
  - gate fail-closed was JUST fixed (2026-06-28) but still single-source

Failures:
  - Futures down → skew_signal.json stale → stocks gate BLOCK_LONG (fixed)
  - But monitor.py still computes its own TAIEX trend → inconsistency possible
  - No dashboard visibility into market state
```

### After (Phase 1)
```
Market State is a shared file:
  - data/market_regime.json written by regime_engine.py
  - market_gate.py reads it (preferred), falls back to skew_signal.json
  - monitor.py reads it (replaces self-computation)
  - Dashboard can read it for regime badge
  - Futures and options are UNCHANGED

Failures:
  - Any single input source fails → degraded but still working
  - Engine crashes → gate fail-closed (just like now)
  - Engine not running → gate uses fallback skew_signal.json
```

## Future (not in Phase 1)

| Feature | When | Why Not Now |
|---|---|---|
| Market breadth (stock scanner aggregated stats) | Phase 2+ | Needs stock scanner to output aggregate metrics first |
| Leadership tracking (2330, 2454, etc.) | Phase 2+ | Needs fixed watchlist with price/volume tracking |
| ETF regime merge (etf_regime_consumer.py) | Phase 2+ | Existing system already works; merge only for consolidation |
| Per-strategy regime response | Not planned | Each strategy already has its own `enabled_regimes` in futures router |
