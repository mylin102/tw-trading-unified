# INCIDENT: Broker session recovery resilience

Status: OPEN
Severity: P1 (risk priority — likely ABOVE telemetry downsampling)
Date: 2026-08-05
Detection: 05:21–05:34 crash loop observed during night-session review
Related: PM2 restarts 163 → 181 (2026-08-04 17:23 → 2026-08-05 05:34)
NOT part of Model C commits (b76f091a / de672ece / 713b19cd / 9b395bc0)

## Summary

Shioaji `list_positions` returned HTTP 500 "Please check param" during
05:21–05:34 on 2026-08-05. `api_is_healthy()` (main.py:555) treated two
failed attempts as session death → `main.py:1297` exited for external
supervisor → PM2 restarted immediately → ~85s later the health check failed
again → 10 restart cycles until the broker API self-recovered at 05:34:41.

**Impact during window: ZERO** — no order intent, no fills, no missed
positions (verified: event ledger 05:20–05:35 has 0 order-related events).

## Root-cause chain (confirmed)

```
Shioaji list_positions -> HTTP 500 "Please check param" (server-side, ~13min window)
→ api_is_healthy() 2 attempts fail
→ main.py:1297 "Shioaji session dead — exiting for external supervisor"
→ PM2 restart (no delay/circuit breaker)
→ new process: login OK, grace period, ~85s later health check again
→ repeat ×10
→ 05:34:41 login succeeds after server recovery → stable (uptime 3h+)
```

- 85s cycle = HEALTH_INTERVAL 30s + 2×1s retry sleep + PM2 boot time
- reconnect owner = PM2 (external supervisor); main.py has no internal
  session-recovery loop — login backoff (15s→120s, 5 attempts) only protects
  the login phase, NOT post-login session death
- restart storm has NO circuit breaker: PM2 configured with no
  min_uptime/max_restarts gating (unstable_restarts=0 despite 10 rapid
  restarts)

## Open questions (must resolve before closing)

1. **Should list_positions 500 be classified as a transient broker error?**
   - 500 with "Please check param" on an unchanged request signature smells
     like server-side transient (maintenance/rate-limit), not a broken client.
   - Proposal: classify 5xx as TRANSIENT_BROKER_ERROR → retry with backoff,
     NOT process exit. Only connection-level failures
     (ShioajiConnectionError / NotReady / socket dead) should trigger
     session-death exit.

2. **Retry/backoff instead of process exit?**
   - api_is_healthy currently: 2 attempts × 1s sleep → exit.
   - Proposal: on transient 5xx, extend to N attempts with exponential
     backoff (e.g. 5s/15s/30s/60s) BEFORE declaring session dead; keep feed
     consumers running (stale-gated) during the probe window.

3. **Restart-storm circuit breaker?**
   - PM2 has no min_uptime/max_restarts — 10 restarts in 13min with no
     gate.
   - Proposal: pm2 config min_uptime=30s, max_restarts=5, restart_delay=15s;
     or in-process counter that backs off to long-duration sleep before exit.

4. **Fail-safe with an open position during session-dead?**
   - This window had no position. If a position existed and the session
     died, current behavior = process exit → PM2 restart → broker state
     recovery → reconciliation. Must verify:
     - restart with position → _restore_position_state from state file
     - fills/broker reconciliation completeness (no ghost/skip)
     - Policy J guard clock survives restart (receive-epoch persisted)
   - Should there be a "hold + alarm" mode instead of blind restart when
     has_position=True?

5. **Position reconciliation after broker recovery — complete?**
   - After 05:34:41 recovery, verify: no duplicate orders, no missed fills,
     no position drift vs broker. (This window: FLAT so trivially clean,
     but the path must be proven for the with-position case.)

## Priority rationale

- Risk is INSTRUMENTAL: a future session-dead with an open position + 10x
  restart storm could produce missed exits, duplicate entries, or
  unreconciled fills. Telemetry downsampling (9b395bc0) reduces disk only.
- Recommend: resolve #1/#2/#3 (transient classification + backoff +
  circuit breaker) before or alongside any telemetry changes; #4/#5 require
  a with-position replay/soak test.

## Action items (proposed, not yet assigned)

- [ ] Classify 5xx as transient broker error in api_is_healthy (main.py:555)
- [ ] Exponential backoff probe before session-dead exit
- [ ] PM2 restart gating (min_uptime / max_restarts / restart_delay)
- [ ] With-position session-dead fail-safe test (replay or paper soak)
- [ ] Post-recovery reconciliation verification test
- [ ] Session-dead alarm/notification (currently silent in PM2 logs only)

## Evidence references

- logs/pm2-trading-out.log 2026-08-05T05:21:37 → 05:34:22 (10× session dead)
- main.py:555 api_is_healthy, main.py:1297 session-dead exit, main.py:46
  RESTART_FLAG, HEALTH_INTERVAL=30 (main.py:47)
- pm2 describe trading-system: restarts=181, unstable_restarts=0
- Event ledger: zero order/fill events 05:20–05:35 (verified)
