# External watchdog design — broker session / process supervision (2026-08-05)
# INCIDENT_broker_session_recovery Phase 5, item 3. DESIGN ONLY — no deploy.

## Problem
PM2 is last-resort process supervision, NOT broker recovery. The 05:10-05:34
restart storm (15 cycles) had: no alert, no circuit-breaker trip (min_uptime
30s < 85s cycle), dashboard stayed "online" (PM2 status) while the trading
process was crash-looping. With an open position, this leaves an unsupervised
position with no human signal.

## Design: cron watchdog (external to trading process)

### Signal sources (all passive, no broker calls)
1. **PM2 status**: `pm2 jlist` → trading-system `status` (online/stopped/
   errored), `restart_time` (monotonic increase = restarts), `pm_uptime`
   (age of current process).
2. **State heartbeat**: /tmp/mts_position_state.json `_updated` — if older
   than N minutes while market open → trading loop stuck (not just restarted).
3. **Restart-storm detector**: if `restart_time` increased by >= 3 within
   10 minutes → STORM.
4. **Position flag**: state file `has_position` (for alert severity).

### Decision table
| Signal | State | Alert | Action |
|---|---|---|---|
| pm2 online, _updated fresh | OK | none | none |
| pm2 online, restart_time +1 in last 10min | RESTART | log | none |
| restart_time +>=3 in 10min | STORM | CRITICAL alert | flag file → dashboard banner; DO NOT stop process |
| pm2 stopped/errored | DOWN | CRITICAL alert | flag file; dashboard TRADING_DOWN; DO NOT auto-start if has_position |
| _updated stale > 5min, market open | STUCK | HIGH alert | flag file |
| has_position + any non-OK | POSITION_AT_RISK | CRITICAL (dedup) | alert; keep process |

### Invariants
- NEVER auto-stop the trading process when has_position=True (leaves
  unsupervised position)
- NEVER auto-restart blindly; alert + flag for human decision (or restart
  only when FLAT and storm detected — operator policy)
- Alerts deduplicated (state change only), not per-minute spam
- Watchdog is read-only: only writes alert flag + log, never touches broker

### Outputs
- /tmp/trading_watchdog_state.json (latest snapshot + alert level)
- logs/watchdog.log (append)
- dashboard banner reads the flag file (existing banner infra)
- Optional: notify to chat platform (gateway cron — TBD)

### Deployment
- cron every 2 min (or launchd 120s KeepAlive=false RunAtLoad)
- independent of trading process; survives trading crash (separate cron/launchd)
- script: scripts/trading_watchdog.py (draft below)

### Open items
- alert delivery channel (dashboard banner only? + Telegram?)
- restart policy for storm-when-FLAT (auto-restart with backoff vs manual)
- reconciliation gate integration (after storm, require reconcile before
  entry re-enable — existing channel_safety RECONCILIATION_PENDING covers)
