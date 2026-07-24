# P0: Release/Exit Phase Isolation (ADR-011)

## Problem

08:52:50 today — both `MTS_RELEASE` and `MTS_EXIT` submitted within 38ms:

```
08:52:50.718  ORDER_SUBMITTED  MTS_RELEASE buy
08:52:50.756  ORDER_SUBMITTED  MTS_EXIT sell
   ↑ no LEG_FILLED between them ↑
```

Root cause: the 500ms quote guard timeout bypass is **shared across all pending decisions**. When it fires, both the RELEASE candidate and the TRAIL candidate (backed up during stale quote) get executed together. The remaining leg exits before release fill is confirmed.

---

## Core Invariant

```
MTS_EXIT is legal
  iff
    lifecycle.phase == SINGLE_LEG
    AND release_group.status == FILLED
    AND remaining_leg is confirmed
```

The SINGLE_LEG transition must ONLY happen via `LEG_FILLED` callback — never at decision time, order submit time, or timeout bypass.

---

## Phase 1: Safety Gate at Order Submission (P0, single file)

Add a hard guard in `_submit_mts_order_signal()` (monitor.py) before any order creation:

```python
# ADR-011: MTS_EXIT requires confirmed SINGLE_LEG
if strategy == "MTS_EXIT":
    _phase = lifecycle.phase
    _rg_status = lifecycle.release_group.status
    if _phase != PositionPhase.SINGLE_LEG or _rg_status != ReleaseGroupStatus.FILLED:
        logger.error(
            "[MTS_RELEASE_EXIT_ILLEGAL] phase=%s rg_status=%s — "
            "MTS_EXIT blocked; only legal after SINGLE_LEG + FILLED",
            _phase.value if _phase else None,
            _rg_status.value if _rg_status else None,
        )
        return  # reject the order
```

**This is the single most impactful guard.** Even if other bugs exist downstream, this prevents the 38ms double-order scenario completely.

**File:** `strategies/futures/monitor.py` — `_submit_mts_order_signal()`

---

## Phase 2: Action-Scoped Timeout Bypass

### 2a. Separate timers per action type

Replace shared `_decision_start_mono` with:

```python
_release_pending_mono: float = 0.0   # starts when RELEASE decision first made
_trail_pending_mono: float = 0.0     # starts when TRAIL decision first made (SINGLE_LEG only)
```

### 2b. Timeout only bypasses its own action

```python
if _decision is not None and _decision.action == LifecycleAction.RELEASE:
    # Release timeout: only bypasses quote guard for the release order
    _elapsed = time.monotonic() - _release_pending_mono
    if _elapsed * 1000 > _release_timeout_ms:
        bypass_quote_guard = True  # only for THIS tick's RELEASE order

if _decision is not None and _decision.action == LifecycleAction.TRAIL:
    # Trail timeout: only bypasses for the trail exit order
    _elapsed = time.monotonic() - _trail_pending_mono
    if _elapsed * 1000 > _trail_timeout_ms:
        bypass_quote_guard = True  # only for THIS tick's TRAIL order
```

### 2c. Reset on phase transition

```python
def on_release_fill(...):
    # ... existing fill logic ...
    _release_pending_mono = 0.0   # clear release timer
    _trail_pending_mono = 0.0     # ensure trail timer starts fresh

def on_flat(...):
    _release_pending_mono = 0.0
    _trail_pending_mono = 0.0
```

**File:** `strategies/plugins/futures/active/tmf_spread.py`

---

## Phase 3: LEG_FILLED as the Only SINGLE_LEG Entry Point

### 3a. Audit all SINGLE_LEG transitions

```bash
grep -n "SINGLE_LEG" strategies/plugins/futures/active/tmf_spread.py
```

Currently at (from earlier reading):
- Line ~1348: `sync_release()` — this is the LEG_FILLED callback path ✅
- Line ~2872-2881: legacy path in `_manage_position()` — **this can transition to SINGLE_LEG without a fill callback** ❌

The legacy path at ~2872:
```python
elif self._released_leg is not None and self._lifecycle_oca.phase not in (SPREAD, SINGLE_LEG):
    self._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        ...
    )
```

This should be hardened to verify that `release_group.status == FILLED` before allowing the transition.

### 3b. Add guard in decision engine

```python
# evaluate_lifecycle_actions(): block TRAIL/TIMEOUT/STOPLOSS candidates
# if release hasn't confirmed fill
if lifecycle.release_group.status != ReleaseGroupStatus.FILLED:
    # Remove any TRAIL candidates
    candidates = [c for c in candidates
                  if c.action not in (LifecycleAction.TRAIL,)]
```

**File:** `strategies/plugins/futures/active/tmf_spread.py`

---

## Phase 4: Post-Fill Trail Re-Arm

In `sync_release()` / fill callback, reset all trail state:

```python
# After fill confirmed, fresh trail start
trail.status = ARMED
trail.armed_at = fill_timestamp
trail.anchor_price = current_remaining_leg_price
self._peak = current_price      # reset peak/nadir
self._nadir = current_price
self._decision_start_mono = 0.0  # clear timeout timers
self._release_far_ticks = 0
self._release_near_ticks = 0
```

Also add a minimum hold:

```python
# P0: post-fill warmup — minimum 500ms + 2 ticks before trail can trigger
self._fill_ts = now
self._fill_mono = time.monotonic()
```

Guard in trail check:

```python
if (time.monotonic() - self._fill_mono) * 1000 < self._trail_warmup_ms:
    self._set_eval(skip_reason="TRAIL_WARMUP")
    return None
```

---

## Phase 5: Tests (✅ Complete)

| Test | What it verifies |
|------|-----------------|
| `test_mts_exit_blocked_before_release_fill` | MTS_EXIT rejected when phase=SPREAD, rg_status=ARMED |
| `test_mts_exit_blocked_when_release_pending` | MTS_EXIT rejected when release SUBMITTED but not FILLED |
| `test_mts_exit_allowed_after_single_leg` | MTS_EXIT passes when phase=SINGLE_LEG, rg_status=FILLED/COMPLETED |
| `test_timeout_bypass_only_current_action` | Release timeout doesn't trigger Trail |
| `test_trail_not_queued_across_ticks` | Trail candidate not preserved when lifecycle changes |
| `test_post_fill_warmup_blocks_immediate_trail` | Trail blocked within 500ms/2 ticks of fill |

## Summary (ADR-011 Complete — 2026-07-16)

| Phase | What | Status |
|-------|------|--------|
| 1 | `_submit_mts_order_signal()` hard gate — phase isolation guard | ✅ |
| 2 | Action-scoped timeout timers (_release_pending_mono, _trail_pending_mono) | ✅ |
| 3 | LEG_FILLED-only SINGLE_LEG + _enter_single_leg_after_release_fill() + legacy path blocked | ✅ |
| 4 | Post-fill warmup (500ms + 2 fresh ticks, AND logic) | ✅ |
| 5 | Tick dedup, restart warmup reset, regression tests 405 pass | ✅ |

### Defense-in-depth chain (38ms double-order now structurally blocked)

```
Release decision ➔ action-scoped timer ➔ Phase 1 gate
    ↓
MTS_RELEASE submitted ➔ LEG_FILLED callback
    ↓
_enter_single_leg_after_release_fill() ➔ full re-arm
    ↓
warmup 500ms + 2 ticks ➔ _trail_pending_mono cleared
    ↓
TRAIL decision ➔ Phase 1 gate verifies SINGLE_LEG + FILLED/COMPLETED
    ↓
MTS_EXIT submitted
```

### Next work: ADR-012 — Restart evidence-based SINGLE_LEG recovery

- fills ledger + broker position must confirm release fill before accepting state file's SINGLE_LEG claim
- Insufficient evidence → RECONCILE_REQUIRED / FROZEN

