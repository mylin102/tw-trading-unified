# ADR: Broker Health, Recovery, and Position Reconciliation Model (DRAFT)

Status: DRAFT (2026-08-05) — pending review, no runtime changes
Related: INCIDENT_broker_session_recovery.md (what happened)
This ADR defines how the system SHOULD behave (future state).
Incident documents the failure; ADR documents the target design.

## Context

2026-08-05 05:10–05:34: 15× PM2 restart storm. Timeline (evidence-based):

```
04:55:01  last normal TickBar (feed alive)
04:59:59  last GCA_TICK (quote stream stops — Shioaji night→day session handoff)
05:00:01  session transition night→day: only cancels pending orders
          (monitor.py:9040-9045) — NO resubscribe / reconnect
05:00-05:09  quote feed silent (GCA_TICK=0), no error logged
05:05+    feed_health ages climb (MXF 315s→375s)
05:10:16  api_is_healthy: list_positions HTTP 500 "Please check param"
          → session-dead → exit → PM2 restart (85s cycle)
05:10-05:34  15 cycles (interval 84-86s deterministic)
05:34:41  broker self-recovers, login OK, stable thereafter
```

Root causes (two independent defects):
1. Shioaji night→day session handoff drops quote subscriptions with no
   resubscribe path (feed dies silently at 05:00).
2. api_is_healthy() treats ANY exception (incl. ServerError 500) as session
   death → process exit; PM2 circuit breaker ineffective because
   (now-created_at) > min_uptime×max_restarts window AND restart survival
   (85s) > min_uptime (30s) → max_restarts never trips.

## Error Taxonomy

| Class | Trigger | Retry | Safe | Initial BO | Max BO | Max consecutive | Relogin | Restart | Has-position behavior | New entry | Existing risk mgmt |
|---|---|---|---|---|---|---|---|---|---|---|---|
| AUTHENTICATION_FAILURE | TokenError/AuthError/AccountNotSign | ✗ | ✗ | — | — | 1 | ✓ | ✓ | block+alert | ✗ | ✗ |
| AUTHORIZATION_FAILURE | 403 | ✗ | ✗ | — | — | 1 | ✓ | ✓ | block+alert | ✗ | ✗ |
| REQUEST_VALIDATION_FAILURE | BadRequest/ValidationError | ✗ | ✗ | — | — | 1 | ✗ | ✗ | log | ✓ | ✓ |
| TRANSIENT_SERVER_5XX | ServerError 5xx | ✓ | ✓ | 5s | 60s | 6 | ✗ | last resort | block-new-entry | ✗ | ✓ |
| RATE_LIMITED | 429 | ✓ | ✓ | 15s | 120s | 4 | ✗ | ✗ | block+throttle | ✗ | ✓ |
| MAINTENANCE | SystemMaintenance | ✓ | ✓ | 60s | 300s | 10 | ✗ | ✗ | block+alert | ✗ | ✓ |
| NETWORK_TIMEOUT | ShioajiTimeoutError | ✓ | ✓ | 5s | 60s | 6 | ✗ | last resort | block+local-risk | ✗ | ✓ |
| CONNECTION_RESET | ShioajiConnectionError/OSError | ✓ | ✓ | 10s | 120s | 5 | ✓ | last resort | block+alert | ✗ | ✓ |
| SDK_SESSION_CLOSED | ShioajiError "session closed" | ✗ | ✗ | 30s | 60s | 3 | ✓ | after relogin | block+alert | ✗ | ✓ |
| MALFORMED_RESPONSE | DecodeError/ResponseError | ✓ | ✓ | 5s | 30s | 3 | ✗ | ✗ | log; never flat-assume | ✗ | ✓ |
| UNKNOWN_BROKER_ERROR | other ShioajiError | ✓ | conservative | 15s | 60s | 3 | conservative | conservative | block+alert | ✗ | ✓ |

500 "Please check param" classification:
- Likely transient broker/server failure: STRONGLY SUPPORTED (500, self-healed,
  same request previously succeeded)
- Exact broker-side cause: UNVERIFIED (no payload capture; SDK C layer)

## Health State Machine

```
HEALTHY ──single timeout/5xx/reset──▶ TRANSIENT_FAILURE
   ▲                                        │
   │ recovery (any success)                 │ consecutive failures ≥ 2
   │                                        ▼
RECOVERED ◀──relogin or probe ok── DEGRADED
                                        │
                                        │ position query ALSO failing +
                                        │ quote/order channels dead
                                        ▼
                                SESSION_INVALID ──relogin fail──▶ PROCESS_RESTART_REQUIRED
```

Transitions (explicit conditions):
- HEALTHY → TRANSIENT_FAILURE: single query failure (any class)
- TRANSIENT_FAILURE → HEALTHY: next query succeeds (counter reset)
- TRANSIENT_FAILURE → DEGRADED: 2+ consecutive failures
- DEGRADED → RECOVERED: any successful query (position or probe)
- DEGRADED → SESSION_INVALID: auth-class error / SDK session closed /
  quote+order channels both dead / relogin probe fails
- SESSION_INVALID → RECOVERED: relogin success + resubscribe success
- SESSION_INVALID → PROCESS_RESTART_REQUIRED: relogin exhausted (3 attempts)
- PROCESS_RESTART_REQUIRED: only from SESSION_INVALID, never from 2 generic
  API failures

Per-state behavior:
- HEALTHY: all normal
- TRANSIENT_FAILURE: bounded retry; no new entry (or policy-gated); existing
  quote feed if healthy → risk mgmt continues
- DEGRADED: new entry blocked; existing position monitored via local risk;
  clear health status; position query unavailable ≠ flat
- SESSION_INVALID: strong evidence required (auth error / session closed /
  channels dead); block everything broker-facing; keep local risk + alert
- PROCESS_RESTART_REQUIRED: last resort; persist state BEFORE exit; on
  restart reconcile first

## Retry/Backoff Policy

- Backoff ladder: 5s → 15s → 30s → 60s (bounded exponential), + jitter
  (±20%) to avoid synchronized retries across processes
- Counter resets to 0 on any success
- NEVER sleep in callback threads; recovery worker is a dedicated daemon
  thread (single worker, generation-id guarded)
- One recovery worker at a time; duplicate login/reconnect prevented
- Each attempt carries a generation id; stale recovery results never
  overwrite a newer healthy state
- With position: new entry blocked; quote processing + local risk +
  emergency/manual exit capability preserved. If order channel ALSO
  unavailable → surface POSITION_OPEN_BROKER_UNREACHABLE (not plain DEGRADED)

## New-Entry Suppression & Existing-Position Behavior

- Any state ≠ HEALTHY → new entry suppressed (strategy entry gates consult
  broker health)
- Existing position: local risk controllers (ATR/hard stop/emergency) keep
  running on local marks; exits that need broker orders are queued/alerted
- Unknown broker state → block new entry (invariant)

## Re-login / Reconnect Policy

- Re-login only from SESSION_INVALID (auth evidence) or CONNECTION_RESET
  after backoff
- Re-login = single worker, generation id, then full resubscribe
  (tick + bidask for futures near/far; MTX; options) — the 05:00 handoff
  gap is exactly this missing resubscribe
- Session transition (night→day): detect quote silence after handoff →
  verify session → resubscribe (NEW)

## Process Restart Criteria

- PROCESS_RESTART_REQUIRED is the ONLY path to self-exit (besides explicit
  signal)
- 2 generic API failures NEVER trigger restart (they trigger DEGRADED +
  retry)
- Before exit: persist strategy state (entry_ts_ms, position, guard) —
  currently exit path does NOT flush state (relies on last _write_mts_state)

## PM2 Circuit Breaker

- PM2 max_restarts window = (now - created_at) < min_uptime × max_restarts
  (God.js:455) — an EARLY-LIFE guard, not a sustained breaker
- CANONICAL config (ecosystem.config.js, HEAD): min_uptime = 120s,
  restart_delay = 15s, max_restarts = 2. min_uptime (120s) > observed 85s
  crash cycle → the storm is classified as unstable and max_restarts trips
  after 2. DO NOT override with a different parameter set.
- PM2 is last-resort process supervision, NOT broker recovery:
  - trading process down → dashboard must show TRADING_DOWN (watchdog)
  - alert via external channel (dashboard banner / notification)
  - manual recovery: pm2 restart; with position → operator protocol
- With position: stopping the restart storm leaves an unsupervised position
  → external watchdog + alert required (see Safety Invariants)

## Position Reconciliation (post-recovery)

Compare 4 sources: broker positions / open orders / fills-deals /
strategy persisted state. Canonical result:
MATCHED | BROKER_ONLY_POSITION | STRATEGY_ONLY_POSITION | QTY_MISMATCH |
SIDE_MISMATCH | CONTRACT_MISMATCH | PENDING_ORDER_UNRESOLVED

- Mismatch → NEVER auto-assume broker or strategy is right; surface for
  operator
- Entry block stays until reconciliation passes (invariant)

## Failure Alerts

- DEGRADED / SESSION_INVALID / PROCESS_RESTART_REQUIRED / with-position +
  broker-unreachable → alert (dashboard banner + log; external channel TBD)
- No silent retry when position open + broker unreachable

## Consequences

- api_is_healthy replaced by broker health state machine (classification +
  backoff)
- Process exit only from SESSION_INVALID after relogin exhaustion
- Session handoff resubscribe added (fixes 05:00 feed-drop)
- PM2 config: min_uptime 120s / max_restarts 5 / restart_delay 15s
- New entry suppressed in any degraded state
- Existing-position risk mgmt preserved; broker-unreachable surfaced
  distinctly
- Reconciliation gate before entry re-enable after recovery

## Rollback Plan

- Revert classification/backoff to 2-attempt-exit (current behavior) in one
  commit
- PM2 params revertible via pm2 restart with old flags
- Resubscribe-on-handoff can be flag-gated off
