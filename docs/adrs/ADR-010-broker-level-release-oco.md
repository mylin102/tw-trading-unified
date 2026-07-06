# ADR-010: Broker-Level Release OCO Bracket

## Status

Proposed (2026-07-06)

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

### State Machine

```text
INACTIVE → ARMED → SUBMITTING → SUBMITTED → PARTIALLY_FILLED → CANCELING_SIBLING → SIBLING_CANCELED → COMPLETED
                                                                                         ↓
                                                                                      FAILED
```

- `SUBMITTING`: one order submitted, second in flight (restartable)
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

- [ ] ReleaseGroupStatus expanded with SUBMITTING, PARTIALLY_FILLED, CANCELING_SIBLING, SIBLING_CANCELED
- [ ] ReleaseGroup has sibling_cancel_order_id, sibling_cancel_status, entry_risk fields
- [ ] CancelStatus enum defined (PENDING, CONFIRMED, REJECTED)
- [ ] EntryRiskSnapshot dataclass defined with serialization
- [ ] All existing ADR-009 tests unchanged
- [ ] Serialization roundtrip: ReleaseGroup → dict → JSON → dict → ReleaseGroup

### Out of Scope (Phase 0)

No behavior changes. Pure data model expansion. The submit/cancel/reconciliation logic is Phase 1+.

## Consequences

Positive:
- Single state machine prevents divergence between two enums
- EntryRiskSnapshot captures all entry-time risk parameters in one object
- SUBMITTING status ensures partial submit is recoverable
- `filled_leg` as winner reduces field redundancy

Negative:
- Larger state file (new fields in release_group)
- Backward compat: old state files without OCO fields → None defaults (safe)
- More complex restart logic (Phase 1+)
