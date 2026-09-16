# Stock quota-aware scanning design

Status: design only; no quota/backoff implementation is included in this change.

## 1. Quota failure classification

The stock monitor should classify a kbar request as quota/transient when the
exception or provider log contains the Sinopac session establishment pattern,
for example:

- `'(c0,s1)_sinopac' to be established`
- `failed to establish` / connection-establishment timeout
- socket timeout, connection reset, or provider rate-limit responses

Classification must be conservative: authentication failures, malformed
contracts, YAML errors, and deterministic code errors are not quota failures
and must not be hidden by a long backoff. Preserve the original exception and
classification in the monitor log/telemetry.

## 2. Backoff policy

Add a per-monitor quota state, reset only after a successful kbar response:

```text
consecutive_quota_failures: 0
next_api_attempt_at: now
base_delay: 30 seconds
max_delay: 30 minutes
```

On each classified quota failure, increment the counter and set:

```text
delay = min(max_delay, base_delay * 2 ** (failures - 1))
next_api_attempt_at = now + delay + bounded_jitter
```

Before making a request, skip the API call while `now < next_api_attempt_at`.
Continue using a valid local cache during the cooldown, marked as
`_trading_disabled=True` as it is today. After five consecutive failures,
enter a degraded scan cadence of at most one API attempt per 30 minutes; do
not spin once per monitor loop. A successful response clears the failure
counter and restores normal cadence. The state should be observable in the
health/diagnostic output so operators can distinguish quota exhaustion from a
dead monitor.

## 3. Cache-first daily indicators

Daily indicators should be cache-first: load the newest valid per-symbol daily
cache before attempting a network kbar request. Refresh a symbol's daily data
at most once per trading day (or when the cache is absent/expired). Intraday
scanning may still use the provider's intraday endpoint, but it must not
re-fetch daily history on every monitor loop. Cache metadata should include
`source`, `fetched_at`, `session_date`, and the request scope so a daily cache
cannot be mistaken for fresh intraday data.

## 4. Planned code seams

- `strategies/stocks/monitor.py`
  - `StockMonitor.run_iteration`: consult quota cooldown before per-symbol
    kbar calls and use cache-first daily data.
  - A new `_classify_kbar_failure` helper: identify only transient/quota
    failures and retain the original exception.
  - A new `_record_quota_failure` / `_record_kbar_success` pair: maintain
    backoff state and telemetry.
  - A new daily-cache loader seam: validate freshness and request scope before
    returning data.
- `strategies/stocks/data_storage.py`
  - Persist/read cache freshness and source metadata without changing the
    existing indicator schema consumers.
- `scripts/runners/stock_runner.py`
  - Keep the configurable outer loop interval separate from API backoff; the
    runner cadence must not reset quota state.
- `tests/strategies/test_stock_monitor_quota.py`
  - Unit tests for classification and exponential backoff boundaries.

## 5. Required test matrix

| Case | Expected result |
| --- | --- |
| First quota failure | failure count 1; next attempt delayed by base delay |
| Consecutive failures | exponential delay, capped at 30 minutes |
| Five or more failures | degraded cadence; no per-loop API retry |
| Quota recovery | successful response clears backoff and permits normal request |
| Non-quota exception | no quota suppression; error remains visible and no false recovery |
| Fresh daily cache | cache used; no daily API request |
| Expired daily cache | one refresh attempt, then cache fallback if transient failure |
| Missing cache | request attempted subject to backoff; no fabricated indicators |
| Cache source/session mismatch | cache rejected or marked non-trading |
| Monitor loop interval change | loop cadence changes without resetting quota state |

This design intentionally leaves implementation pending approval.
