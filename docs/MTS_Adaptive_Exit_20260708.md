# MTS Exit Optimization Roadmap (Execution Order)

## Goal

Improve exit quality while minimizing risk to the existing ADR-010 lifecycle and OCO state machine.

**Principles**

* One feature per PR.
* Every feature must be independently testable.
* Do not mix data plumbing with strategy logic.
* Do not modify lifecycle/state machine unless explicitly required.

---

# Priority P1 — Session Risk Controls

## Objective

Never leave unintended directional exposure after the configured session close.

### Scope

* `_minutes_to_session_close()`
* SINGLE_LEG force exit (last 5 minutes)
* Settlement day force exit (13:30+)
* Explicit exit signal builder
* Idempotency guard

### Design

* Risk control belongs in `monitor._mts_tick()`
* NOT inside strategy
* NOT inside lifecycle engine

### Acceptance

* SINGLE_LEG within 5 minutes

  * exactly one `MTS_EXIT`
* SPREAD

  * no trigger
* FLAT

  * no trigger
* EXITING

  * no duplicate orders
* Settlement day after 13:30

  * all phases force flat

---

# Priority P2 — Activate Existing VWAP Exit

## Objective

Enable the already implemented VWAP exit logic.

Current status:

```text
_apply_vwap_exit()
        ✓ exists

near_vwap/far_vwap
        ✗ never injected
```

Result:

VWAP exit is effectively disabled.

---

## Scope

Only modify data plumbing.

Inside `_mts_tick()`:

```python
bar["near_vwap"] = near_vwap
bar["far_vwap"] = far_vwap
```

VWAP should be calculated from the rolling tick-bar deque already maintained by the monitor.

---

## Do NOT

* modify `_apply_vwap_exit()`
* change exit thresholds
* introduce new strategy logic

This PR only activates an existing feature.

---

## Guards

Inject `None` when:

* insufficient bars
* zero volume
* invalid VWAP
* NaN
* stale data

VWAP exit should automatically no-op in those cases.

---

## Acceptance

* bar contains `near_vwap`
* bar contains `far_vwap`
* SINGLE_LEG near uses `near_vwap`
* SINGLE_LEG far uses `far_vwap`
* SPREAD phase unchanged
* Existing unit tests continue passing

---

# Priority P3 — Profit Lock Ladder

## Objective

Reduce profit give-back after release.

Current issue:

Large unrealized profit can return to near breakeven before a wide trailing stop activates.

---

## Example

```text
Floating Profit

10 pts
    trail = 8

20 pts
    trail = 5

30 pts
    trail = 3

40 pts
    trail = 2
```

The ladder only tightens.

It never loosens.

---

## Principle

Profit protection should become increasingly aggressive as unrealized profit grows.

---

## Acceptance

* trail never widens
* ladder is monotonic
* configurable
* deterministic
* independent of VWAP

---

# Priority P4 — Adaptive Stop

## Objective

Adapt release stop width to market volatility.

---

## Regime Detection

Use Bollinger Bandwidth.

Example:

```text
Low Vol
    stop = 0.8 ATR

Normal
    stop = 1.0 ATR

High Vol
    stop = 1.3 ATR
```

---

## Scope

Release Stop only.

Do NOT modify:

* trailing stop
* lifecycle
* OCO
* state transitions

---

## Acceptance

* deterministic regime classification
* configurable thresholds
* unit tests for each regime

---

# Priority P5 — Grid Sweep & Calibration

## Objective

Find production parameters using historical testing.

---

## Parameters

Sweep:

* ATR stop multiplier
* trail multiplier
* VWAP tighten ratio
* Profit Lock Ladder
* Adaptive Stop thresholds

---

## Metrics

Evaluate:

* Net PnL
* Max Drawdown
* Profit Factor
* Win Rate
* Average Trade
* Sharpe (optional)

---

## Output

Generate ranked parameter tables.

No production code changes.

---

# Priority P6 — Production Calibration

Only after Grid Sweep.

Update production config with validated parameters.

Example:

```yaml
atr_multiplier_stop:
trail_distance:
vwap_tighten_ratio:
profit_lock:
adaptive_stop:
```

No strategy logic changes.

Configuration only.

---

# Architectural Rules

## Keep Strategy Pure

Strategy decides:

* entry
* release
* trail

Monitor handles:

* session risk
* settlement risk
* force exit
* VWAP data injection

---

## Separate Data from Logic

Data PR:

* VWAP injection

Logic PR:

* Profit Lock
* Adaptive Stop

Never combine both.

---

## Lifecycle Safety

Do not modify:

* ADR-010 state machine
* OCO lifecycle
* reconciliation
* restart recovery

unless explicitly required.

---

# Recommended Implementation Order

```text
P1  Session Risk Controls
        ↓
P2  VWAP Data Injection
        ↓
P3  Profit Lock Ladder
        ↓
P4  Adaptive Stop
        ↓
P5  Grid Sweep
        ↓
P6  Production Calibration
```

This order minimizes regression risk while enabling incremental validation of each enhancement before progressing to the next.

