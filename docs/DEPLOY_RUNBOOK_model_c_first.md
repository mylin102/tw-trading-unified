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

### Maintenance entry lock — sequencing (2026-08-05 CORRECTION)

The maintenance_entry_lock.flag only takes effect once the NEW runtime
(release 806b19f1) loads it. The OLD runtime does NOT read the flag — so
creating the flag early does NOT prevent re-entry on the old process.

Correct sequence:

```
1. lock flag: data/maintenance_entry_lock.flag — status: "PREPARED,
   takes effect on new runtime load" (NOT "active")
2. current position ends naturally -> six-item VERIFIED_FLAT
3. IMMEDIATELY: pm2 stop trading-system   (old code must not re-enter)
4. confirm: PID gone, broker=0, working orders=0
5. start release trading-system (--only, canonical config)
   - MUST start from a PERMANENT, IMMUTABLE release path (NOT /private/tmp,
     NOT a repo release/ subdir):
       /Users/myllin_mini/Documents/mylin102/tw-trading-unified-releases/
       <full-commit-sha>/
   - This deploy: .../releases/3d2e6ecb78d00ed25a4eed255518437ca57e70a5/
   - PM2 cwd MUST be the physical commit-SHA path (identity-stable). A
     `current` symlink MAY exist for human inspection ONLY — never use it
     as PM2 cwd (symlink switch would cause identity drift).
   - Verify pm2 describe trading-system -> exec cwd == the physical SHA
     path above, script == taskpolicy, args resolve under the same path.
6. NEW runtime reads the EXISTING flag -> every entry evaluation on the
   new runtime reads the lock, so it will not create a new ENTRY.
   Existing positions can still only exit naturally / via risk controls.
7. 120s observe-only (gate 3 execution-boundary checks)
8. lock is held until ALL of: post-start broker reconciliation, the four
   subscriptions, AND the 120s soak pass — then DELETE flag (entry
   resumes). The lock is NOT released merely because the process started.
```

Abort rule: if a NEW ENTRY appears between FLAT confirmation and `pm2
stop` (the ~5min re-entry interval), ABORT immediately — do not force the
switch; wait for the next natural FLAT.

The ~5min re-entry interval after flatten is normally enough to complete
steps 3-5; if it is not, abort and wait.

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

## Persistent Runtime Data/Log Layout (2026-08-05)

Runtime data and logs live OUTSIDE the immutable release tree, in a
persistent repo-external location:

```
/Users/myllin_mini/Documents/mylin102/tw-trading-unified-runtime/
    data/          # telemetry, state, flags (persistent, writable)
    logs/          # pm2 out/err, event ledger (persistent, writable)
```

Inside the release tree, controlled links point to the persistent location:
- <release>/data  ->  runtime/data   (or per-file links)
- <release>/logs  ->  runtime/logs
- <release>/.env  ->  read-only controlled secrets (symlink to secrets
  store; must NOT be writable by the release tree)

CRITICAL: <release>/data/maintenance_entry_lock.flag MUST be the path the
new runtime actually reads (5-dirname resolution lands on <release>/data/).
Verify after start: the running process's lock helper resolves to
<release>/data/maintenance_entry_lock.flag and sees the flag present.

## Reboot Gate (P0 — pm2 save discipline)

pm2 save MUST NOT be executed while any position is open (would persist the
wrong path). Save only AFTER the new runtime is verified, in this order:

1. VERIFIED_FLAT (six-item) — required before ANY of the following
2. create release tree at the physical SHA path (above)
3. stop old trading-system -> confirm PID=0
4. start the single new process from the release path
5. pm2 describe verification:
   - cwd == physical SHA path
   - args == main.py --config futures (single config)
   - min_uptime == 120s, restart_delay == 15s, max_restarts == 2
6. runtime HEAD == release commit; lock flag present at <release>/data/
7. 120s soak (execution-boundary checks, gate 3)
8. THEN pm2 save
9. verify dump.pm2: trading-system entry cwd == the SAME physical SHA
   path, canonical params; launchd resurrect would restore this exact path

Any of these failing -> do NOT pm2 save; roll back to the previous
known-good runtime (record its SHA before starting).

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
