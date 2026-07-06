# P3 Data Ingestion Pipeline

## Three-Layer Architecture

### [P1] Raw Tick Writer / Runtime Cache
**File:** `strategies/futures/squeeze_futures/data/tick_writer.py`

- Raw tick CSV is the **truth source**
- Deque is a **runtime performance cache only**
- `write()` must be called **before** any in-memory cache update
- After a crash, market-data state can be rebuilt from raw tick CSV plus persisted raw kbars (P2)
- flushed every 100 records for I/O balance

### [P2] Canonical Bar Rebuild
**File:** `core/bar_utils.py`

- `build_preferred_canonical_bar_frames()` is the **canonical bar selector**
- Priority order: **tick-5m > api-1m > legacy-api-5m**
- Strategy consumes canonical bars only — never raw API responses
- `_strategy_tick()` is a **pure data consumer**, never a fetcher

### [P3] Recovery Watchdog
**File:** `strategies/futures/monitor.py`

- **`fetch_legacy_fallback()`** is used by: scheduled recovery / ingestion watchdog only
- **`fetch_legacy_fallback()`** is NOT used by: `_strategy_tick()` — strategy_tick is a pure data consumer, never a fetcher
- `_strategy_tick()` must never fetch — it only reads via `_get_tick_bars_df()` / `_periodic_backfill_bars()`
- PM2 restart count must remain stable during market open

---

## P3 Acceptance Criteria

1. **`_strategy_tick()` has zero `fetch_*` calls** — all data comes from pre-built in-memory structures
2. **`_strategy_tick()` does not import or call Shioaji API** — no `api.kbars`, no `client.get_kline`
3. **`fetch_legacy_fallback()` only runs from watchdog / scheduled recovery** — never from strategy tick
4. **Fetched fallback data is persisted before use** — CSV written before canonical bar rebuild
5. **Canonical bars are updated before strategy reads them** — `build_preferred_canonical_bar_frames()` runs before any strategy `on_bar()`
6. **No API response is passed directly into `StrategyContext`** — all data flows through canonical bar pipeline
7. **PM2 restart count remains stable during market open** — no crashes from stale/dead data feeds

---

## Data Flow Diagram

```
                          ┌─────────────────────┐
                          │   Shioaji Tick Feed  │
                          └──────────┬──────────┘
                                     │
                                     ▼
                    ┌────────────────────────────┐
                    │  [P1] RawTickWriter.write() │  ← CSV first, then cache
                    │  CSV (truth source)         │
                    │  Deque (runtime cache)      │
                    └────────────┬───────────────┘
                                 │
                   ┌─────────────┴─────────────┐
                   │                           │
                   ▼                           ▼
        ┌──────────────────┐     ┌────────────────────┐
        │ tick-5m bars     │     │ [P2] api-1m bars   │
        │ (from deque)     │     │ (from IngestionSvc) │
        └────────┬─────────┘     └─────────┬──────────┘
                 │                         │
                 └──────────┬──────────────┘
                            ▼
           ┌───────────────────────────────────┐
           │ build_preferred_canonical_bar_    │
           │ frames(candidates=[tick-5m, ...]) │  ← P2 selector
           └───────────────┬───────────────────┘
                           │
                           ▼
                  ┌──────────────────┐
                  │ Canonical Frames │  ← [P3] Watchdog feeds
                  │ (5m / 15m / 1h)  │    fallback data here
                  └────────┬─────────┘
                           │
                           ▼
                    ┌──────────────┐
                    │ Strategy     │
                    │ on_bar()     │  ← pure consumer
                    └──────────────┘
```

## Implementation Notes

- Rate limit for IngestionService fetch: 120s (in `fetch_backfill()`)
- Rate limit for legacy fallback: 300s (in `ingestion_service.py:139`)
- Minimum 5m bars for strategy readiness: 2 (configurable via `min_5m_bars`)
- MXF feed staleness threshold: 120s (configurable via `STALE_WARN_SECS`)

---

## P4 Hardening Items

### [P4-1] Canonical Freshness SLA
**Method:** `_check_canonical_freshness()` in `monitor.py`

- Checks if the last canonical 5m bar is older than the SLA threshold (default: 600s = 2× bar interval)
- If stale, appends `"STALE_DATA"` to `MarketData.flags` (via `_data_flags` on `FuturesMonitor`)
- **Never fetches, never crashes, never blocks trading** — purely observational
- SLA configurable via `CANONICAL_SLA_SECS` attribute
- Stale → fresh recovery detection also logs a recovery message

### [P4-2] Structured Watchdog Logging
**Applied to:** `_check_futures_contract_staleness()` in `monitor.py`

All watchdog actions log in a unified grep-able format:

```
[IngestionWatchdog] reason=<reason> symbol=<sym> tick_age_secs=<N>
last_bar_ts=<ts> canonical_age_secs=<N> action=<action> result=<result>
```

Supported `reason` values:
- `feed_stale` — tick age exceeds warn threshold, degraded mode set
- `feed_stale_critical` — tick age exceeds critical, triggers supervisor restart
- `market_closed` — market closed during recess, keep-alive only
- `contract_expired` — delivery date passed, triggering rollover
- `rollover_failed` — rollover/resubscribe attempt failed

Supported `action` values: `check_contract`, `rollover`, `fetch_recovery_kline`, `shutdown`, `light_recovery`, `none`
Supported `result` values: `degraded`, `trigger_supervisor_restart`, `market_closed_keep_alive`, `attempting`, `triggered`, `success:rows=N`, `empty_response`, `exception:<msg>`

### [P4-3] tick-5m vs api-1m Cross-Source Consistency Check
**Method:** `_check_tick_api_consistency()` in `monitor.py`

- Compares the latest bar close between tick-5m (P1) and api-1m (P2) sources
- Runs **periodically** (every 30 ticks) — never every tick
- Warns if close price difference exceeds `MAX_TICK_POINT_DISCREPANCY` (default 5.0 MXF points)
- Logs in structured `[IngestionWatchdog]` format with `reason=tick_api_mismatch`
- **Never fetches, never crashes, never blocks trading** — purely observational
- Fields: `tick_close`, `api_close`, `diff`, `threshold`, `tick_last_ts`, `api_last_ts`, `active_source`

