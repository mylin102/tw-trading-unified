# ADR-009 v1.1: Position Lifecycle OCA (ReleaseGroup + TrailGroup)

## Status

Accepted (2026-07-03)

## Changelog (v1.1)

- Clarified ReleaseGroup FILLED vs COMPLETED semantics
- Added FULL_EXIT transition path for double-release fill
- Removed ambiguous FAILED, defined as terminal only
- Added atomic commit requirement (write failure cancels submission)
- Added invariants for phase/group consistency
- Separated phase (position shape) from group status (workflow progress)

## Context

MTS spread exit logic currently suffers from:
- Race conditions between release and trail triggering simultaneously
- `_released_leg` desync between strategy instance and state file
- Crash → restart → duplicate order submission
- `_pending_lifecycle_orders` lost on restart
- No single source of truth for exit lifecycle

The root cause is ad-hoc exit decision code spread across `_manage_position()`:
release checks and trail checks run independently in the same tick, each directly
submitting orders without arbitration.

## Decision

Replace ad-hoc exit logic with a two-group state machine:

```
PositionLifecycle
    ├── phase (position shape: FLAT | SPREAD | SINGLE_LEG)
    ├── ReleaseGroup (OCO for the two spread legs)
    └── TrailGroup (post-release single-leg exit manager)
```

### PositionPhase

Indicates the **shape** of the position, not the order progress.

```
FLAT        — no position
SPREAD      — both legs held (near + far)
SINGLE_LEG  — one leg released, one remaining
```

### ReleaseGroupStatus

Tracks the lifecycle of the release OCO pair.

```
INACTIVE    — not in spread phase
ARMED       — spread held, monitoring release_stop threshold
TRIGGERED   — release_stop hit, about to submit orders
SUBMITTED   — both release orders submitted to broker
FILLED      — one release leg filled, sibling cancel pending
COMPLETED   — filled leg confirmed and sibling cancel confirmed
FAILED      — terminal failure (reject / timeout / API error)
```

### TrailGroupStatus

Tracks the post-release single-leg trailing exit.

```
INACTIVE    — not in single-leg phase
ARMED       — release confirmed, trail not yet active
ACTIVE      — trail stop calculated and monitored
SUBMITTED   — trail/exit order submitted
FILLED      — exit fill confirmed
FAILED      — terminal failure
```

### Lifecycle Controller

All exit decisions follow a strict pipeline:

```
collect candidates → select by priority → commit → submit
```

Priority order:

```
MANUAL > STOPLOSS > TIMEOUT > RELEASE > TRAIL
```

Each evaluation cycle selects **at most one** action. The selected action is
persisted to state file **before** any order submission (`commit before submit`).

**Commit must be atomic.** Either the state file is written successfully, or
no order submission occurs. A failed commit must not result in an orphan order.

### State File SSOT

```json
{
  "has_position": true,
  "position_phase": "SPREAD",
  "release_group": {
    "status": "SUBMITTED",
    "near_order_id": "ORD-001",
    "far_order_id": "ORD-002",
    "filled_leg": null,
    "filled_order_id": null,
    "canceled_leg": null,
    "trigger_ts": "2026-07-03T10:00:00"
  },
  "trail_group": {
    "status": "INACTIVE"
  }
}
```

### State Transitions

```
FLAT
  │  entry submitted
  ▼
SPREAD  (ReleaseGroup: ARMED)
  │  release_stop threshold hit
  ▼
SPREAD  (ReleaseGroup: TRIGGERED)
  │  orders submitted
  ▼
SPREAD  (ReleaseGroup: SUBMITTED)
  │  one leg filled
  ▼
SPREAD  (ReleaseGroup: FILLED, canceling sibling)
  │  sibling cancelled → position reduced
  ▼
SINGLE_LEG  (ReleaseGroup: COMPLETED, TrailGroup: ARMED)
  │  trail condition met → trail armed
  ▼
SINGLE_LEG  (TrailGroup: ACTIVE)
  │  trail stop hit → exit order submitted
  ▼
SINGLE_LEG  (TrailGroup: SUBMITTED)
  │  exit fill confirmed
  ▼
FLAT  (all groups: INACTIVE)
```

### Error Transitions

```
RELEASE_SUBMITTED
  │
  ├── one leg filled → SINGLE_LEG (sibling cancelled)
  │
  └── both legs filled → FULL_EXIT → FLAT (no TrailGroup)

Any SUBMITTED state + timeout → FAILED
```

### Invariants

```
phase == FLAT
    ⇒ ReleaseGroup == INACTIVE ∧ TrailGroup == INACTIVE

phase == SPREAD
    ⇒ TrailGroup == INACTIVE

phase == SINGLE_LEG
    ⇒ ReleaseGroup == COMPLETED
```

### Key Rules

1. Release is only valid when `phase == SPREAD && release_group.status == ARMED`
2. Trail is only valid when `phase == SINGLE_LEG && trail_group.status in (ARMED, ACTIVE)`
3. One action per evaluation cycle (collect → select → commit → submit)
4. State file is written BEFORE order submission; write failure cancels submission
5. On restart: incomplete lifecycle enters reconciliation (no path re-selection)
6. Manual and StopLoss dominate all other actions regardless of phase
7. Sibling double-fill (both release orders fill) → FULL_EXIT → FLAT
8. Invariants must hold at all stable states (after each completed transition)

### Backward Compatibility (Task 2)

State files without lifecycle block infer phase from legacy fields:
- `has_position == False` → FLAT
- `has_position == True && release_state in (NEAR_RELEASED, FAR_RELEASED)` → SINGLE_LEG
- `has_position == True && release_state == BOTH_HELD` → SPREAD

## Consequences

Positive:
- Single source of truth for exit state
- No race between release and trail
- Crash recovery without duplicate orders
- Clean separation: phase = position shape, group status = order progress
- Testable in isolation (pure state machine)
- Invariants enable runtime assertion checking

Negative:
- Larger state file (nested dict)
- Migration needed for existing positions in state file
- Client-side OCO has brief window where sibling can also fill

## Task Plan

```
Task 0: State diagram (this document)
Task 1: Data model & enums
Task 2: State persistence + backward compat inference
Task 3: Lifecycle controller (evaluate_lifecycle_actions)
Task 4: _manage_position integration
Task 5: Remove legacy decision state
Task 6: Restart reconciliation
Task 7: Callback integration
```
