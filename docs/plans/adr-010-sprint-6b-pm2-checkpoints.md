# ADR-010 Sprint 6B: PM2 Restart Checkpoint Verification

## Goal

Verify that PM2 restart preserves all OCO lifecycle states correctly during a live paper session.

## What This Verifies

The `_restore_position_state()` method (Sprint 5 reconciliation) handles 5 restart scenarios (FI-1 through FI-5). Sprint 6B proves these work under real PM2 conditions — not just in unit tests — at every OCO lifecycle checkpoint.

## Checkpoint States to Verify

| # | OCO State | Restart Expectation | FI Test |
|---|-----------|-------------------|---------|
| A | **SUBMITTED** (both order ids live) | Restore SUBMITTED, no re-submit | FI-1 |
| B | **SUBMITTING** (near order id only) | Restore SUBMITTING, `_reconcile_release_bracket_submission` | FI-4 |
| C | **PARTIALLY_FILLED** (one filled, cancel not submitted) | Restore PARTIALLY_FILLED, trail INACTIVE | FI-5 |
| D | **CANCELING_SIBLING** (cancel in flight) | → SIBLING_CANCELED → SINGLE_LEG + trail ARMED | FI-2 |
| E | **SIBLING_CANCELED** | → SINGLE_LEG + trail ARMED | FI-3 |
| F | **SINGLE_LEG + trail ARMED** | Restore SINGLE_LEG, trail ARMED | implicit |

## Procedure

### Prerequisites

```bash
# Ensure correct branch and latest code
cd /Users/mylin/Documents/mylin102/tw-trading-unified
git checkout adr-010-release-oco-bracket
git pull

# Full test pass first
python3 -m pytest tests/ -k "adr_010 or mts_exit or tmf_spread" -q
# Expected: all pass
```

### Checkpoint A: SUBMITTED → PM2 restart

1. Enable paper mode in config
2. Start PM2: `pm2 start ecosystem.config.js`
3. Let MTS enter a spread and submit both OCO release orders
4. **Capture checkpoint**:
   - Run: `python3 scripts/debug/oco_checkpoint.py capture`
   - Verify state file contains:
     - `lifecycle.release_group.status = "SUBMITTED"`
     - `lifecycle.release_group.near_order_id` and `far_order_id` both populated
5. **PM2 restart**: `pm2 restart trading-system`
6. **Verify**: Watch PM2 logs — expected output:
   - `[OCO_RESTORE_5B] SUBMITTED — near=... far=...`
7. **Validate**: run `python3 scripts/debug/oco_checkpoint.py verify` — confirms:
   - `lifecycle.release_group.status == "SUBMITTED"`
   - `near_order_id` and `far_order_id` preserved (same ids as checkpoint)
   - `trail_group.status == "INACTIVE"`

### Checkpoint B: PARTIALLY_FILLED → PM2 restart

1. After Checkpoint A passes, wait for first release order fill
2. **Capture checkpoint** before cancel is submitted:
   - State: `release_group.status == "PARTIALLY_FILLED"`, `filled_leg` populated
   - `trail_group.status == "INACTIVE"`
3. **PM2 restart**: `pm2 restart trading-system`
4. **Verify**: State restores as PARTIALLY_FILLED, trail stays INACTIVE
5. The system should then submit the sibling cancel on the next tick (via `_check_oco_release_fill` + `_mts_tick` 4C logic)

### Checkpoint C: CANCELING_SIBLING → PM2 restart

1. After PARTIALLY_FILLED + cancel submitted
2. **Capture checkpoint** before cancel confirmation processed:
   - State: `release_group.status == "CANCELING_SIBLING"`
   - `sibling_cancel_status == "PENDING"`
3. **PM2 restart**
4. **Verify**: Unit test FI-2 expects → SIBLING_CANCELED → SINGLE_LEG + trail ARMED
   - Live: `_restore_position_state()` runs FI-2/5A logic → promotes to SINGLE_LEG + trail ARMED
   - PM2 logs: `[OCO_RESTORE_5A] CANCELING_SIBLING → SIBLING_CANCELED → SINGLE_LEG`

### Checkpoint D: SIBLING_CANCELED → PM2 restart

1. After sibling cancel confirmed
2. **Capture checkpoint**:
   - State: `release_group.status == "SIBLING_CANCELED"`
   - `phase == "SPREAD"` (in state file; 5D promotes to SINGLE_LEG at restore time)
3. **PM2 restart**
4. **Verify**: 
   - PM2 logs: `[OCO_RESTORE_5D] SIBLING_CANCELED → SINGLE_LEG + trail ARMED`
   - State after restore: `phase == "SINGLE_LEG"`, `trail_group.status == "ARMED"`

### Checkpoint E: SINGLE_LEG + trail ARMED → PM2 restart

1. After trail is active on remaining leg
2. **Capture checkpoint**:
   - `phase == "SINGLE_LEG"`, `trail_group.status == "ARMED"`
3. **PM2 restart**
4. **Verify**: State preserved, trail resumes on next tick

## Edge Cases

### Edge: PM2 restart during SUBMITTING (partial submit)

This is a very brief window — only the first release order has been submitted.

- **Expected behavior**: `_reconcile_release_bracket_submission()` handles partial submit
- **Verification**: If caught by checkpoint, state has `far_order_id == None`
- **No re-submit guard**: The helper must NOT submit a duplicate near order

### Edge: PM2 kill -9 (SIGKILL) during CANCELING_SIBLING

- State file may not reflect the latest write
- **Recovery**: `_read_mts_state()` gets the last fsynced state
- **Fallback**: If state file is stale or empty, broker order status query is authority
- **Manual check**: `scripts/debug/oco_checkpoint.py dump` to inspect what PM2 picks up

### Edge: Back-to-back PM2 restart (double restart)

1. Restart PM2 once — state restores correctly
2. Immediately restart PM2 again before any tick arrives
3. **Expected**: Second restart reads the same state, no crash, no duplicate logic
4. **Guard**: `_restore_position_state()` returns False if already restored (`_has_position == True`)

## Verification Script: oco_checkpoint.py

Located at `scripts/debug/oco_checkpoint.py`. Usage:

```bash
# Capture snapshot of current lifecycle state
python3 scripts/debug/oco_checkpoint.py capture

# Dump current state (human-readable)
python3 scripts/debug/oco_checkpoint.py dump

# Verify state integrity after restart
python3 scripts/debug/oco_checkpoint.py verify [--checkpoint <path>]
```

The script:
- Reads `/tmp/mts_position_state.json`
- Extracts `lifecycle` block
- `capture`: saves a labeled snapshot to `_oco_checkpoints/<timestamp>_<label>.json`
- `verify`: compares current state against snapshot, reports any discrepancies
- Works independently — no PM2 or trading system dependency

## Acceptance Criteria

```text
✅ A: SUBMITTED → PM2 restart → state preserved, no duplicate orders
✅ B: PARTIALLY_FILLED → PM2 restart → state preserved, cancel proceeds
✅ C: CANCELING_SIBLING → PM2 restart → promoted to SINGLE_LEG + trail ARMED
✅ D: SIBLING_CANCELED → PM2 restart → promoted to SINGLE_LEG + trail ARMED
✅ E: SINGLE_LEG/trail ARMED → PM2 restart → state preserved
✅ Edge: SUBMITTING → restart → reconcile helper called
✅ Edge: double PM2 restart → idempotent
✅ oco_checkpoint.py functional: capture, dump, verify
```

## Rollback

If any checkpoint fails:
1. Capture state file: `cp /tmp/mts_position_state.json /tmp/mts_position_state.6b_fail.json`
2. Kill PM2: `pm2 kill`
3. Fix root cause + re-test with FI unit tests
4. Retry checkpoint procedure
