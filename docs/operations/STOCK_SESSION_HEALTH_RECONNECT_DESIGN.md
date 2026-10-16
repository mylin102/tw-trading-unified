# Stock Shioaji session health and reconnect design

Status: design only; no session/reconnect implementation is included here.

## 1. Failure signature and detection

The observed failure is a dead Shioaji session, not an API quota exhaustion:

```text
api/v1/data/kbars, code: NotReady, SubCode(SessionNotEstablished),
error_str: Unable to wait for session '(c0,s1)_sinopac' to be established
```

The stock monitor must classify a session as unhealthy when an exception or
provider response contains `SessionNotEstablished` or `NotReady`, or when
kbars calls fail consecutively at least `N` times. A single transient failure
must not trigger reconnect. Suggested initial threshold: `N=3`, configurable
for tests and operations. Track the consecutive failure count, last failure
signature, and timestamp in monitor health state.

## 2. Reconnect supervision

After the threshold is reached, enter `SESSION_INVALID`/reconnect state and
stop issuing normal kbar requests until the next reconnect attempt. Use
bounded exponential backoff, for example:

```text
base_delay = 5 seconds
delay = min(5 minutes, base_delay * 2 ** (attempt - 1))
max_attempts = 5
```

Add bounded jitter to avoid synchronized retries. Each reconnect attempt must
be observable in the log with attempt number and reason. After the maximum
attempts, emit an error/alert and remain degraded; do not retry forever in the
one-second loop. A later operator-controlled restart or explicitly permitted
recovery action can reset the supervisor.

On successful session/login and a successful kbar request, clear the failure
counter and reconnect attempt state, return to normal polling, and log the
recovery. Credentials and `live_trading` semantics must remain unchanged.

## 3. Relationship to futures supervision

The futures side already models `session dead → SESSION_INVALID → reconnect`,
with PM2 `autorestart` and `stop_exit_codes=[0]`. Stock cannot copy that wiring
directly because it has a different runner and a different loop:

- futures owns a larger session/router lifecycle;
- stock uses `scripts/runners/stock_runner.py` and calls
  `StockMonitor.run_iteration()` in its own loop;
- restarting a stock process has different state and order-safety concerns.

Stock should reuse the concepts—not the futures implementation details:

- explicit session health state and terminal/degraded distinction;
- thresholded failure detection rather than single-error reconnect;
- bounded exponential reconnect attempts;
- success/failure telemetry and operator-visible alerts;
- PM2 behavior that treats clean exit (`0`) differently from a crash.

## 4. Degraded operation and cache truthfulness

While the session is invalid or reconnect backoff is active, the monitor may
continue using a valid local cache for dashboard/reference indicators. Cached
data must retain the existing `_data_source="cache"` and
`_trading_disabled=True` markers. It must never be presented as fresh API data
and must not authorize a trade. If no valid cache exists, report missing data
and skip the symbol; do not fabricate indicators.

## 5. Planned code seams

- `strategies/stocks/monitor.py`
  - classify kbar exceptions/responses;
  - maintain session health and consecutive-failure state;
  - gate kbar requests during backoff;
  - invoke a bounded reconnect callback and emit recovery/failure alerts;
  - preserve cache fallback truthfulness.
- `scripts/runners/stock_runner.py`
  - own the stock loop's supervision boundary and sleep cadence;
  - preserve the configured interval without resetting session state;
  - expose clean-vs-error termination for PM2 supervision.
- `core/shioaji_session.py`
  - provide a narrow reconnect/login seam using existing credentials and
    connection setup, without changing live-mode policy.
- PM2 ecosystem configuration
  - audit only after code approval; align restart policy with stock runner
    exit semantics.

## 6. Required test matrix

| Case | Expected result |
| --- | --- |
| One `NotReady`/`SessionNotEstablished` error | count increments; no immediate reconnect |
| Consecutive failures below `N` | normal threshold behavior; no reconnect |
| Failure count reaches `N` | session marked invalid; normal kbar calls gated |
| Reconnect succeeds | state/counters reset; normal polling resumes |
| Reconnect repeatedly fails | bounded attempts, backoff cap, alert, no infinite retry |
| Session recovers after degraded period | successful kbar clears degraded state |
| Valid cache during reconnect | cache used and clearly marked CACHE/trading disabled |
| Expired or missing cache | no fabricated data; symbol skipped/alerted |
| Configured loop interval | cadence changes without resetting health state |

Implementation requires explicit approval before modifying code or PM2 config.
