# P0: `decision=None` Root Cause Diagnosis Plan

## Status

In Progress — diagnostic log deployed, awaiting first tick at 08:45 CST.

---

## Problem

`_manage_position()` consistently logs:

```
MTS_RELEASE_EVAL
  far_hit=True
  decision=None
  tick_ct=10544/2     (confirm passed 10k+ ticks ago)
  quote_age=0/2000.0  (fresh)
```

`evaluate_lifecycle_actions()` returns `None` even when:
- `far_pnl_pts = -208` >> `release_stop_threshold = 158.4`
- `rg_status = ARMED`
- `phase = SPREAD`

Unit test `test_release_eval_bug.py` **cannot reproduce** with the exact same numeric values. The function works correctly in isolation.

→ Bug is in **runtime state of `self._lifecycle_oca`** that deserialization creates differently from the test's fresh construction.

---

## Current Diagnostic (Already Deployed)

`_check_release_candidates()` at line 457 now logs `[CHECK_RELEASE_SKIP]` with reason, phase, status on **every early return**. It logs `[CHECK_RELEASE_DECISION]` on success.

### What we'll see on first tick

| Log pattern | Meaning | Action |
|---|---|---|
| `[CHECK_RELEASE_DECISION] action=RELEASE leg=FAR` | function works, `_decision` gets set | Quote guard A+B+C will activate; tick confirmation passes after 2nd tick |
| `[CHECK_RELEASE_SKIP] phase=X expected=SPREAD` | `lifecycle.phase` is wrong (e.g. FLAT, SINGLE_LEG) | Check `_restore_position_state()` → `lifecycle_from_dict` or `infer_lifecycle` path |
| `[CHECK_RELEASE_SKIP] rg_status=X expected in (ARMED,TRIGGERED)` | `release_group.status` is wrong (e.g. TRIGGERED, SUBMITTED, COMPLETED) | State file was written prematurely; check `_write_mts_state` + `_commit_action` sequence |
| `[CHECK_RELEASE_SKIP] near_hit=False far_hit=False` | Threshold condition not met (unlikely given live PnL) | Check `_evaluate_risk` output vs actual PnL |
| No `[CHECK_RELEASE_*]` log at all | `evaluate_lifecycle_actions` never reaches `_check_release_candidates` | In-flight guard in `evaluate_lifecycle_actions` blocks (line 549 or 554); need diagnostic there too |

---

## Phase 1: After First Tick (Immediate)

### 1a. Read `[CHECK_RELEASE_*]` from PM2 log

```bash
grep "CHECK_RELEASE" logs/pm2-trading-*.log
```

### 1b. Read current state file

```bash
python3 -c "
import json
s=json.load(open('/tmp/mts_position_state.json'))
lc=s.get('lifecycle',{})
rg=lc.get('release_group',{})
print(f'phase={lc.get(\"phase\")}')
print(f'rg_status={rg.get(\"status\")}')
print(f'filled_leg={rg.get(\"filled_leg\")}')
print(f'near_order_id={rg.get(\"near_order_id\")}')
print(f'far_order_id={rg.get(\"far_order_id\")}')
print(f'trigger_ts={rg.get(\"trigger_ts\")}')
print(f'state_revision={s.get(\"state_revision\")}')
"
```

### 1c. Decision matrix

| `[CHECK_RELEASE_*]` result | Root cause | Fix |
|---|---|---|
| **DECISION** (works now) | PM2 restart somehow repaired the lifecycle state | Proceed to P1: Quote guard refinement |
| **SKIP rg_status != ARMED** | State file lifecycle was written with bad status | Fix lifecycle serialization/deserialization path |
| **SKIP phase != SPREAD** | Lifecycle phase corrupted on restore | Fix `_restore_position_state` → `lifecycle_from_dict` |
| **No log at all** | Blocked before `_check_release_candidates` in `evaluate_lifecycle_actions` | Add `[LIFECYCLE_INFLIGHT_GUARD]` diagnostic before return None at lines 549-557 |

---

## Phase 2: Fix Root Cause (Code Change)

### 2a. If rg_status or phase wrong on restore

Example: state file says `ARMED` but `lifecycle_from_dict` produces INACTIVE.

**Hypothesis**: `_release_group_from_dict` at line 244 creates a new `ReleaseGroup(status=_enum_from_value(...))`. If `d.get("status")` returns None or unexpected value, it defaults to `INACTIVE`.

**Fix path**: 
- Add `_enum_from_value` logging on fallback
- Add `[LIFECYCLE_RESTORE_DIAG]` in `lifecycle_from_dict` and `infer_lifecycle_from_legacy_state` to log what was parsed

### 2b. If phase wrong (e.g. SINGLE_LEG or FLAT)

**Hypothesis**: The legacy `infer_lifecycle_from_legacy_state` at line 1906 overrides the correct `lifecycle_from_dict` result.

**Fix path**:
- Check line 1879: the `elif` condition `state["lifecycle"]["release_group"]["status"] not in (None, "INACTIVE", "ARMED", "TRIGGERED")` — if the state file has an unexpected status value, this condition may evaluate incorrectly, forcing `_pollute_pass = False` even though lifecycle is valid.
- Add `[RESTORE_POLLUTE_CHECK]` log at line 1879 showing what the status string is and whether `_pollute_pass` was computed correctly.

### 2c. If no `CHECK_RELEASE_*` log at all (blocked in evaluate_lifecycle_actions)

Add diagnostic at lines 549 and 554:

```python
# Line 549: In-flight release guard
if lifecycle.release_group.status in (SUBMITTED, FILLED):
    logger.warning("[LIFECYCLE_INFLIGHT_GUARD] reason=RELEASE_IN_FLIGHT status=%s",
                   lifecycle.release_group.status)
    return None

# Line 554: In-flight trail guard
if lifecycle.trail_group.status in (SUBMITTED, FILLED):
    logger.warning("[LIFECYCLE_INFLIGHT_GUARD] reason=TRAIL_IN_FLIGHT status=%s",
                   lifecycle.trail_group.status)
    return None
```

---

## Phase 3: Prevent Recurrence

### 3a. Add Invariant Assertion (Paper Mode Only)

Add after line 2568:

```python
if (
    _decision is None
    and self._lifecycle_oca.phase == PositionPhase.SPREAD
    and self._lifecycle_oca.release_group.status == ReleaseGroupStatus.ARMED
    and _n_pnl <= -_release_stop
):
    logger.error(
        "[MTS_RELEASE_DECISION_INVARIANT_VIOLATION] "
        "far_hit=True but evaluate_lifecycle_actions returned None — P0"
    )
```

In paper mode, this should raise `RuntimeError(...)` to catch regressions immediately.

### 3b. Audit lifecycle serialization round-trip

Write a test that:
1. Reads the actual state file
2. Deserializes via `lifecycle_from_dict`
3. Calls `evaluate_lifecycle_actions` with actual PnL values
4. Asserts decision is not None

This catches state-file corruption before the next restart.

---

## Phase 4 (After P0 Fixed): P1 — Quote Guard Enhancement

Only after `decision=None` is resolved:

### 4a. Review existing A+B+C implementation (already in code lines 2576-2666)

Verify:
- Decoupled Leg Freshness Check ✓ (lines 2596-2622)
- 500ms Timeout Force Release ✓ (lines 2657-2659)
- 1.5x Stop Bypass ✓ (lines 2634-2655)

### 4b. Add missing `_decision_start_mono` init in `__init__` and `sync_position`

Check that `self._decision_start_mono` is initialized to `0.0` in:
- `__init__()` 
- `sync_position()` (line ~1297 area)

### 4c. Run existing quote guard tests

```bash
pytest tests/strategies/test_mts_quote_guard_bypass.py -v
```

---

## Timeline

| Time | Event |
|---|---|
| 08:17 | Current time (market closed) |
| 08:45 | Day session opens, first tick arrives |
| 08:45+1s | READ `[CHECK_RELEASE_*]` log → determine root cause |
| 08:46 | Code fix based on diagnostic |
| 08:47 | PM2 restart with fix |
| 08:48 | Position releases (far hit threshold, decision produced) |
