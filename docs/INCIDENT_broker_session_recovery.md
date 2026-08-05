# INCIDENT: Broker session recovery resilience

Status: OPEN
Severity: P1 (risk priority — likely ABOVE telemetry downsampling)
Date: 2026-08-05
Detection: 05:10–05:34 crash loop observed during night-session review
Related: PM2 restarts 163 → 181 (2026-08-04 17:23 → 2026-08-05 05:34)
NOT part of Model C commits (b76f091a / de672ece / 713b19cd / 9b395bc0)

## Summary

Shioaji `list_positions` returned HTTP 500 "Please check param" during
05:10–05:34 on 2026-08-05 (24 minutes). `api_is_healthy()` (main.py:555)
treated two failed attempts as session death → `main.py:1297` exited for
external supervisor → PM2 restarted immediately → ~85s later the health
check failed again → **15 restart cycles** until the broker API
self-recovered at 05:34:41.

**Impact during window: ZERO** — no order intent, no fills, no missed
positions (verified: event ledger 05:10–05:35 has 0 order-related events;
state file stayed FLAT throughout — restore path never exercised).

**Causal framing (2026-08-05 correction)**: the session transition lacking a
resubscribe path, followed by feed silence (GCA_TICK=0 05:00–05:09), then
list_positions 500 + restart storm, STRONGLY SUPPORTS a causal link between
the handoff gap and the failure. The exact broker-side cause remains
UNVERIFIED — we have not confirmed that subscription loss caused the session
to become invalid at the broker.

## Root-cause chain (confirmed)

```
Shioaji list_positions -> HTTP 500 "Please check param" (server-side, 24min window)
→ api_is_healthy() 2 attempts fail (main.py:555-575)
→ main.py:1297 "Shioaji session dead — exiting for external supervisor"
→ PM2 restart (no delay/circuit breaker)
→ new process: login OK, ~55s warmup, 30s health interval → fail again
→ repeat ×15 (05:10:16 … 05:34:22, interval 84–86s REGULAR)
→ 05:34:41 login succeeds after server recovery → stable (uptime 3h+)
```

- **85s cycle decomposition**: HEALTH_INTERVAL 30s (main.py:47) + 2×1s
  retry sleep + ~55s boot/warmup (login + contract load + warmup) = 84–86s
  observed. Fully deterministic.
- **retry owner = PM2 (external supervisor)**: main.py has NO internal
  session-recovery loop. Login backoff (15s→120s, 5 attempts, main.py:820)
  only protects the login phase — post-login session death always goes to
  PM2 restart.
- **no circuit breaker**: PM2 config has no min_uptime/max_restarts gating
  (unstable_restarts=0 despite 15 rapid restarts in 24min).
- **500 classification**: "Please check param" on an UNCHANGED request
  signature = server-side transient (maintenance/rate-limit), not a client
  bug. Current code cannot distinguish 5xx-transient from connection-dead.

## Open questions (must resolve before closing)

1. **Should list_positions 500 be classified as a transient broker error?**
   - 5xx on unchanged request = server transient. Proposal: classify 5xx as
     TRANSIENT_BROKER_ERROR → retry with backoff, NOT process exit. Only
     connection-level failures (ShioajiConnectionError / NotReady / socket
     dead) should trigger session-death exit.

2. **Retry/backoff instead of process exit?**
   - api_is_healthy currently: 2 attempts × 1s sleep → exit.
   - Proposal: on transient 5xx, extend to N attempts with exponential
     backoff (5s/15s/30s/60s) BEFORE declaring session dead; keep feed
     consumers running (stale-gated) during the probe window.

3. **Restart-storm circuit breaker?**
   - PM2 has no min_uptime/max_restarts — 15 restarts in 24min with no
     gate.
   - Proposal: pm2 config min_uptime=30s, max_restarts=5, restart_delay=15s;
     or in-process counter that backs off to long-duration sleep before exit.

4. **Fail-safe with an open position during session-dead?**
   - This window had NO position (state FLAT throughout; restore path never
     exercised). If a position existed:
     - restart → _restore_position_state from state file
     - fills/broker reconciliation completeness (no ghost/skip)
     - Policy J guard clock survives restart (receive-epoch persisted)
   - Should there be a "hold + alarm" mode instead of blind restart when
     has_position=True?

5. **Position reconciliation after broker recovery — complete?**
   - After 05:34:41 recovery: no duplicate orders, no missed fills, no
     position drift vs broker. This window was FLAT (trivially clean) —
     the with-position path must be proven via replay/soak.

## Priority rationale

- Risk is INSTRUMENTAL: a future session-dead with an open position + 15x
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

- logs/pm2-trading-out.log 2026-08-05T05:10:16 → 05:34:22 (15× session dead,
  interval 84–86s)
- main.py:555 api_is_healthy, main.py:1297 session-dead exit, main.py:46
  RESTART_FLAG, HEALTH_INTERVAL=30 (main.py:47)
- pm2 describe trading-system: restarts=181, unstable_restarts=0, current
  process start 05:34:40
- Event ledger: zero order/fill events 05:10–05:35 (verified)
- State file: FLAT throughout the window (no restore triggered)
