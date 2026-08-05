# Deployment Runbook — Model C First Deploy (flat-gated)

Target: `release/model-c-first-deploy` @ **8e1da2f6f4098d3fda1751a3a1b067f93f470716**
(remote + local verified identical)
Date: 2026-08-05

## Flat Gate (must ALL hold before ANY deployment action)

```
- state file: has_position=False, trade_id=None
- broker position = 0
- working orders = 0
- lifecycle = INACTIVE (or FLAT terminal per state machine)
- PM2 trading-system: online, no active entries
```

## Hard Gate 1 — Clean checkout, never overwrite dirty tree

- DO NOT `git checkout` in the production working tree:
  production tree has dirty runtime data (data/current, data/tmf_full_2026.csv,
  rebuild_logs) — checkout would clobber or conflict.
- Release branch is ALREADY pushed:
  `origin/release/model-c-first-deploy` = 8e1da2f6 (verified).
- Use a CLEAN deployment worktree:
  ```
  git worktree add /tmp/_deploy_mc1 --detach origin/release/model-c-first-deploy
  cd /tmp/_deploy_mc1
  git rev-parse HEAD   # must print 8e1da2f6...
  ```
- Verify the worktree contains the pinned SHA BEFORE stopping anything.

## Hard Gate 2 — stop → PID gone → single start (NO pm2 restart)

- `pm2 restart` is FORBIDDEN as a deploy action (ambiguous, may double-start
  or touch dashboard/stock-runner).
- Sequence:
  ```
  # 1. stop ONLY trading-system
  pm2 stop trading-system

  # 2. confirm PID gone (no brief double instance)
  pgrep -f "main.py --config futures" || echo "no trading process"
  #   (expect empty; also check: no python holding the old code)

  # 3. single start with canonical config, ONLY trading-system
  pm2 start ecosystem.config.js --only trading-system
  #   (ecosystem.config.js carries min_uptime=120s / restart_delay=15s /
  #    max_restarts=2 from the release worktree)

  # 4. confirm single instance + new code
  pm2 jlist | grep -A2 trading-system   # restarts=0 for this boot
  pgrep -f "main.py --config futures" | wc -l   # expect 1
  ```
- dashboard and stock-runner are NOT touched (—only flag).

## Hard Gate 3 — Observe-only must have a VERIFIABLE execution boundary

Watchdog OK is NOT sufficient. Verify ALL of:

1. **Model C influence flags** (telemetry, unchanged):
   - grep model_c telemetry: `execution_influence=false`, `order_influence=false`
2. **Trading mode NOT switched to live**:
   - state file: `mode` field == paper (config futures.yaml `mode: paper` or
     dashboard banner PAPER MODE)
   - no `live_transition` / go-live marker active
3. **No new entry intent**:
   - no `MTS_ENTRY` order_submitted events during observation
   - strategy eval trace shows entries blocked/skipped (safety gate reason)
4. **No order routing**:
   - zero ORDER_SUBMITTED/ORDER_UPDATE events in event ledger during soak
5. **Classifier state**:
   - broker health state stays HEALTHY/TRANSIENT (never
     PROCESS_RESTART_REQUIRED)
6. **PM2 restarts = 0** during observation window

Only when ALL of 1-6 hold for the soak window is the deploy considered
observe-only-pass.

## Soak Checklist (observe-only window, >= one day/night session transition)

```
□ near tick counter continuously increasing
□ far tick counter continuously increasing
□ near bidask counter continuously increasing
□ far bidask counter continuously increasing
□ quote timestamps continuously updating
□ spread values updating normally
□ quote age within expected threshold
□ no callback silence
□ watchdog status = OK
□ classifier never enters PROCESS_RESTART_REQUIRED
□ PM2 restart count = 0 during observation
□ execution boundary verified (gate 3 items 1-6)
□ day/night transition observed PASSIVELY (never manually disconnect/
  restart session for the test — only observe the natural handoff)
```

## Day/Night Transition Validation

- ONLY observe the natural 15:00 night open / 05:00 day open handoff.
- Verify resubscribe fires: `[TRANSITION_RESUB]` lines in log after the
  session-type change.
- Verify feed continues: no GCA silence window, tick+bidask counters keep
  rising across the boundary.
- NEVER simulate by disconnecting or killing the session.

## Post-Soak

- If soak passes: keep the release running; schedule 9b395bc0 (bounded
  observation) as a SEPARATE deployment.
- If any gate fails: stop trading-system, record evidence, revert by
  checking out the previous pinned SHA (record before starting).

## Rollback

- Previous runtime SHA: `1ced1bd3` (HEAD before this deploy sequence;
  verify against pm2 uptime + git reflog at deploy time).
- Rollback = stop → PID check → start previous SHA worktree (same gate 2
  sequence). Never `pm2 restart`.
