# MTS State Machines (Layered Architecture)

Two independent state machines that communicate via events.

```
Lifecycle SM            Policy J SM
(持倉生命週期)           (組合停利決策)
                   
FLAT                 IDLE
  │                    │
  ▼                    │
ENTRY_PENDING          │
  │                    │
  ▼                    │
SPREAD ─────────────── MONITORING
  │         ┌────────► │
  │         │          ▼
  │         │       ARMED
  │         │          │
  ▼         │          ▼
SINGLE_LEG  │     TRIGGERED
  │         │          │
  ▼         └──────────┘
EXIT_PENDING
  │
  ▼
FLAT
```

---

## Layer 1: Lifecycle State Machine

Owns: position state, orders, fills, settlement.

### States

| State | has_position | open_qty | entry_allowed | Description |
|-------|-------------|----------|---------------|-------------|
| FLAT | False | 0/0 | True | No position |
| ENTRY_PENDING | False→True | filling | False | Entry orders submitted, awaiting fills |
| SPREAD | True | 1/1 | False | Both legs held, monitoring exits |
| SINGLE_LEG | True | 0/1 or 1/0 | False | One leg released, trailing the other |
| EXIT_PENDING | True | filling | False | Exit orders submitted, awaiting fills |

### Transitions

| Event | Guard | From | To | Side Effect |
|-------|-------|------|----|-------------|
| ENTRY_AUDIT | spread_z >= 3.0, flat | FLAT | FLAT | emit ENTRY_AUDIT |
| ENTRY_SUBMIT | audit passed | FLAT | ENTRY_PENDING | OrderManager.create_order() x2, _save_orders_file() |
| LEG_FILLED (both) | both filled | ENTRY_PENDING | **SPREAD** | emit ENTRY, ReleaseGroup → ARMED, emit ENTRY_ESTABLISHED → Policy J |
| RELEASE_STOP_HIT | release gate | SPREAD | SPREAD | ReleaseGroup → TRIGGERED, submit OCO orders |
| LEG_FILLED (1 of OCO) | OCO filled | SPREAD | **SINGLE_LEG** | cancel sibling, emit RELEASE |
| EXIT_SUBMIT (trail) | trail gate | SINGLE_LEG | SINGLE_LEG | order_mgr.submit() |
| EXIT_FILLED | trail filled | SINGLE_LEG | **EXIT_PENDING** | emit EXIT |
| EXIT_FILLED (all legs) | all exit orders filled | EXIT_PENDING | **FLAT** | settlement, has_position=False |
| COMBINED_EXIT_REQUEST ← Policy J | both exit orders submitted | SPREAD | **EXIT_PENDING** | OrderManager.create_order() x2, _save_orders_file() |
| COMBINED_EXIT_REQUEST ← Policy J | exit order submitted | SINGLE_LEG | **EXIT_PENDING** | OrderManager.create_order() |

### Invariants

```
1. FLAT ⇒ has_position=False, near_open=0, far_open=0
2. ENTRY_PENDING ⇒ exit orders = 0
3. SPREAD ⇒ near_open=1, far_open=1
4. SINGLE_LEG ⇒ exactly one leg open
5. EXIT_PENDING ⇒ entry blocked, settlement pending
6. LEG_FILLED event always includes order_id, price, qty
7. All order mutations go through OrderManager canonical path
8. Order state persisted via _save_orders_file_wrapper()
```

---

## Layer 2: Policy J State Machine

Owns: peak tracking, activation, giveback detection.

### States

| State | peak_tracking | eligible | activated | would_trigger | Description |
|-------|--------------|----------|-----------|---------------|-------------|
| IDLE | stopped | False | False | False | No position |
| WARMUP | blocked | False | False | False | Position established, waiting for stable valuation |
| MONITORING | active | True | False | False | Peak updating, below activation threshold |
| ARMED | active | True | True | False | Peak >= activation, monitoring giveback |
| TRIGGERED | stopped | False | True | True | Giveback condition met, exit submitted |

### Transitions

| Event | Guard | From | To | Side Effect |
|-------|-------|------|----|-------------|
| ENTRY_ESTABLISHED ← Lifecycle | both legs filled | IDLE | **WARMUP** | _entry_established_at = now |
| WARMUP_EXPIRED | elapsed >= warmup_ms (3s) | WARMUP | **MONITORING** | peak tracking begins |
| PEAK_UPDATE | current > peak | MONITORING | MONITORING | _peak = current |
| ACTIVATION | peak >= 200 TWD | MONITORING | **ARMED** | emit ACTIVATED event (Dashboard) |
| PEAK_UPDATE (during ARMED) | current > peak | ARMED | ARMED | _peak = current |
| GIVEBACK | current < peak - 50 TWD | ARMED | **TRIGGERED** | emit COMBINED_EXIT_REQUEST → Lifecycle |
| FLAT_EVENT ← Lifecycle | has_position=False | any | **IDLE** | reset peak=0, _entry_established_at=None |

### Guards

```
eligible = (position_phase == SPREAD
            AND near_open_qty > 0
            AND far_open_qty > 0)

warmup_complete = (now - _entry_established_at) * 1000
                  >= policy_j_entry_warmup_ms  (default 3000)

activated = peak_net_exit_pnl_twd >= activation_threshold  (200 TWD)

giveback_triggered = activated
                     AND current_net_exit_twd < exit_line_twd

exit_line_twd = max(0, peak_net_exit_pnl_twd - giveback_twd)  (50 TWD)
```

### Invariants

```
1. IDLE ⇒ peak=0, _entry_established_at=None
2. WARMUP ⇒ peak unchanged, activated=False, would_trigger=False
3. MONITORING ⇒ peak tracks current, activated=False
4. ARMED ⇒ peak tracks current, activated=True, would_trigger=False
5. TRIGGERED ⇒ exit submitted, entry blocked
6. peak ONLY updated when eligible=True AND warmup_complete=True
7. COMBINED_EXIT_REQUEST emitted at most once per position
8. No dependency on strategy._far_open_qty for completion decision
```

---

## Cross-Layer Event Bus

Lifecycle and Policy J communicate through typed events — no direct method calls.

| Event | Producer | Consumer | Payload |
|-------|----------|----------|---------|
| ENTRY_ESTABLISHED | Lifecycle (SPREAD) | Policy J → WARMUP | timestamp, near_entry, far_entry |
| COMBINED_EXIT_REQUEST | Policy J (TRIGGERED) | Lifecycle → EXIT_PENDING | trade_id, activation, current, peak, giveback |
| FLAT_EVENT | Lifecycle (FLAT) | Policy J → IDLE | trade_id, final_pnl |

This decoupling means:
- Adding a new exit policy (e.g., trailing stop, volatility breakout) only requires a new SM that listens to Lifecycle events and emits EXIT_REQUEST.
- Lifecycle never needs to know which policy triggered the exit.
- Policy decisions can be unit-tested independently of order execution.

---

## Defect Regression Guards (CI-enforced)

| ID | Condition | Must NOT happen | Applies to |
|----|-----------|-----------------|------------|
| D1 | strategy._far_open_qty==0 AND FAR filled_qty==0 | COMBINED_EXIT_COMPLETED, FLAT | Lifecycle EXIT_PENDING |
| D2 | 1 leg filled during ENTRY_PENDING | peak update, activated=True | Policy J WARMUP |
| D3 | 1 leg filled in EXIT_PENDING (COMBINED_EXIT) | COMBINED_EXIT_COMPLETED, FLAT | Lifecycle EXIT_PENDING |
| D4 | Snapshot missing/stale | Dashboard local recompute | Policy J (observability) |
| D5 | Partial position (warmup in progress) | peak update | Policy J WARMUP |
