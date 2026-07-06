# ADR-010: Broker-Level Release OCO Bracket

## Status

Implements ADR-010. All sprints 1-6B complete. (2026-07-06)

## Context

ADR-009 established a lifecycle-controlled release mechanism where the lifecycle controller selects one leg and submits a single release order. This is sufficient for most spread scenarios, but it is not broker-level OCO:

- Only one release order is submitted at a time
- No two-sided bracket exists for simultaneous fill competition
- Restart during submit has no protection for partial submission
- No entry-time risk snapshot to prevent ATR drift during bracket lifetime

For production-grade MTS trading, entry fills should immediately deploy a two-sided release OCO bracket: submit both near and far release orders simultaneously, let the market decide which leg exits first, cancel the sibling order, and activate trail on the remaining leg.

## Decision

Extend the existing ADR-009 lifecycle controller with broker-level release OCO bracket support:

### Design Principles

1. **Single state machine** — `ReleaseGroupStatus` is the sole state machine for release lifecycle. No separate OcoStatus enum. `filled_leg` IS the winner_leg, `canceled_leg` IS the loser leg.

2. **Bracket encapsulated in order_mgr** — `OrderManager.submit_release_bracket()` is the single entry point. Broker-specific OCO logic (Shioaji native, emulated, or paper mock) lives inside order_mgr. Strategy/monitor never call `create_order()` directly for release orders.

3. **Entry risk snapshot** — Risk parameters at entry time are captured in `EntryRiskSnapshot` dataclass to prevent ATR drift during the bracket lifetime.

4. **Two-phase SINGLE_LEG** — Fill callback sets `PARTIALLY_FILLED`, cancel sibling in flight (`CANCELING_SIBLING`), only after sibling cancel confirmed (`SIBLING_CANCELED`) does trail activate.

5. **Restart covers SUBMITTING** — Partial submit (one order id persisted, one missing) is a first-class restart scenario.

6. **Atomic state persistence** — `ReleaseGroupStatus` and order ids must be updated atomically with state persistence. A `SUBMITTED` release group without both `near_order_id` and `far_order_id` is invalid.

### State Machine

```text
INACTIVE
  → ARMED
  → SUBMITTING
  → SUBMITTED
  → PARTIALLY_FILLED
  → CANCELING_SIBLING
  → SIBLING_CANCELED
  → COMPLETED

Any non-terminal state → FAILED
```

- `SUBMITTING`: bracket submission has started but both release order ids are not yet durably persisted (restartable)
- `SUBMITTED`: both order ids persisted
- `PARTIALLY_FILLED`: one leg filled, sibling cancel not yet confirmed
- `CANCELING_SIBLING`: cancel submitted, awaiting confirmation
- `SIBLING_CANCELED`: cancel confirmed, safe to enter SINGLE_LEG
- `FAILED`: both legs filled / cancel rejected / unrecoverable

### Extended ReleaseGroup Fields

```python
@dataclass
class ReleaseGroup:
    status: ReleaseGroupStatus           # single state machine
    near_order_id: str | None
    far_order_id: str | None
    filled_leg: Leg | None              # IS the winner_leg
    filled_order_id: str | None
    canceled_leg: Leg | None            # IS the loser leg
    trigger_ts: str | None
    # ADR-010
    sibling_cancel_order_id: str | None
    sibling_cancel_status: CancelStatus | None  # PENDING | CONFIRMED | REJECTED
    entry_risk: EntryRiskSnapshot | None
```

### EntryRiskSnapshot

```python
@dataclass
class EntryRiskSnapshot:
    atr: float
    release_stop: float
    trail_stop: float
    entry_z: float
    spread: float
    timestamp: str
```

### Restart Scenarios

| Scenario | State | Recovery |
|----------|-------|----------|
| A | SUBMITTED, both order ids | Broker check → restore or re-drive |
| B | SUBMITTING, one order id | Broker check → finish submit or rollback |
| C | CANCELING_SIBLING | Check cancel status → CONFIRMED or FAILED |
| D | SIBLING_CANCELED | Verify broker position → restore SINGLE_LEG |

### Acceptance Criteria (Phase 0)

- [x] ReleaseGroupStatus expanded with SUBMITTING, PARTIALLY_FILLED, CANCELING_SIBLING, SIBLING_CANCELED
- [x] ReleaseGroup has sibling_cancel_order_id, sibling_cancel_status, entry_risk fields
- [x] CancelStatus enum defined (PENDING, CONFIRMED, REJECTED)
- [x] EntryRiskSnapshot dataclass defined with serialization
- [x] All existing ADR-009 tests unchanged
- [x] Serialization roundtrip: ReleaseGroup → dict → JSON → dict → ReleaseGroup

### Out of Scope (Phase 0)

No behavior changes. Pure data model expansion. The submit/cancel/reconciliation logic is Phase 1+.

## Consequences

Positive:
- Single state machine prevents divergence between two enums
- EntryRiskSnapshot captures all entry-time risk parameters in one object
- SUBMITTING status ensures partial submit is recoverable
- `filled_leg` as winner reduces field redundancy
- Sprint 6A complete: synthetic paper lifecycle roundtrip verified
- Sprint 6B complete: PM2 restart checkpoint plan + oco_checkpoint.py utility

Negative:
- Larger state file (new fields in release_group)
- Backward compat: old state files without OCO fields → None defaults (safe)
- More complex restart logic (Phase 1+)

## Lessons Learned: Paper Execution Engine Pump Gap (2026-07-06)

### Root Cause

ORD-000003/ORD-000004 were orphaned in `paper_fill_sim._pending_orders` despite
correct lifecycle state transitions. The OCO state machine correctly handled
fill events (SUBMITTED → PARTIALLY_FILLED → CANCELING_SIBLING), but **no fill
events were ever generated** because nobody called `process_tick()` after the
initial submission-time synthetic tick.

### The 4-Layer Checklist

Future OCO / order lifecycle reviews must verify ALL four layers, not just the
state machine:

| Layer | Check | Verified By |
|-------|-------|-------------|
| 1. Registration | Order enters pending queue (status=SUBMITTED) | Unit test: `sim.register(order)` |
| 2. Pump | Tick loop calls `process_tick()` with live prices | Integration test: `_mts_tick()` → `_process_pending_paper_fills()` |
| 3. Callback | Fill triggers `on_fill` → lifecycle transition | Acceptance test: fill event → `_check_oco_release_fill` |
| 4. Persistence | Transition written to state file | State file assertion: `release_group.status` after fill |

Prior tests covered layers 1, 3, and 4. Layer 2 was the blind spot that caused
this incident.

### Submission-Time Synthetic Tick = False Safety

The initial `process_tick(synthetic_tick)` at OCO submission time gives the
illusion that paper fill is connected, but:

- `data.get("near_fill_price", 0)` silently falls back to 0 if the entry
  fill price is missing
- The outer `except Exception` swallows errors without `logger.exception`
- Both cause the order to appear SUBMITTED while never filling

**Fix**: fail-fast guard (`raise ValueError` for price ≤ 0), ongoing polling
in `_mts_tick()`, and `logger.exception` in all paper fill error paths.

### Acceptance Test Pattern

```text
submit OCO → leave pending → run _mts_tick() → one leg fills →
sibling cancels → state persists with correct lifecycle
```

See `tests/test_order_lifecycle/test_paper_fill.py::TestPaperFillSimulatorPolling`.
