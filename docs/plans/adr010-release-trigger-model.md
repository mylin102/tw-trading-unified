# ADR-010 Follow-up: MTS Release OCO Trigger Model

## Status

Accepted (2026-07-07)

---

# Problem

The current implementation models `MTS_RELEASE_OCO` as two executable orders submitted immediately after entry.

This is fundamentally incorrect.

Release OCO is **not** a resting order.

It is a **trigger condition** (ATR-based stop/release).

Submitting the orders immediately causes premature fills because the orders become executable before the release condition has actually been satisfied.

Changing the order type from MKP to LMT does **not** solve the underlying problem.

---

# Root Cause

Current flow:

```
Entry filled
    ↓
calculate ATR release threshold
    ↓
submit_release_bracket()
    ↓
Near order submitted
Far order submitted
    ↓
paper_fill / broker
    ↓
immediate execution possible
```

The trigger condition is bypassed.

The broker (or paper simulator) becomes responsible for deciding when the order executes.

This is opposite of the intended strategy.

---

# Correct Model

Release OCO should be a **synthetic strategy-level trigger**.

The strategy decides **when** to submit the release order.

The broker only executes the order after it has been submitted.

```
Entry filled
    ↓
calculate ATR release threshold
    ↓
release_group = ARMED
    ↓
wait for market ticks
    ↓
threshold reached?
        │
    no ───────────────► keep waiting
        │
       yes
        │
        ▼
submit ONE release order
        │
confirmed fill
        │
transition to SINGLE_LEG
        │
arm trailing exit
```

---

# Design Principles

## Release OCO is NOT a broker order

It is a strategy state.

Before threshold is reached:

- no release order exists
- no paper order exists
- no broker order exists

Only strategy state exists.

```
release_group.status = ARMED
```

---

## Broker should only execute

Broker responsibilities:

- receive order
- acknowledge order
- fill order

Broker should **not** decide when the ATR stop is triggered.

---

## Trigger precedes execution

Correct sequence:

```
Trigger
    ↓
Submit
    ↓
Fill
```

Never:

```
Submit
    ↓
Wait for trigger
```

---

# Required Behaviour

## After Entry Fill

```
Entry confirmed
```

System shall:

- snapshot entry spread
- snapshot ATR release threshold
- create release_group
- set status = ARMED

System shall NOT:

- submit release orders
- register release orders
- create pending paper orders

---

## Every Tick

While

```
phase == SPREAD
```

and

```
release_group.status == ARMED
```

evaluate

```
current spread
```

against

```
entry spread
```

using the ATR release threshold.

---

## Threshold Not Reached

Nothing happens.

No order is created.

No broker interaction occurs.

---

## Threshold Reached

Exactly one side is selected.

Example:

```
Near release triggered
```

Immediately:

```
release_group.status = SUBMITTING
```

Submit exactly one release order.

Example:

```
Sell Near
```

No sibling release order should ever be submitted.

---

## Fill

After confirmed execution:

```
filled_leg = NEAR
```

Transition:

```
SPREAD
    ↓
SINGLE_LEG
```

Enable trailing logic.

---

# State Machine

```
ENTRY FILLED
        │
        ▼
     ARMED
        │
        │
        │ (tick)
        ▼
Threshold Hit
        │
        ▼
 SUBMITTING
        │
        ▼
  FILLED
        │
        ▼
 SINGLE_LEG
        │
        ▼
 TRAILING
```

---

# Invalid Behaviour

The following is incorrect.

```
Entry
    ↓
submit two LMT orders
    ↓
wait
```

Reasons:

- LMT can become immediately marketable.
- MKP certainly executes immediately.
- Trigger logic is bypassed.
- Strategy loses control of execution timing.

Changing

```
MKP
```

to

```
LMT
```

does not solve the architectural issue.

---

# Implementation Notes

Release threshold should be calculated once after entry.

Example:

```
release_stop =
ATR × atr_mult_stop
```

Store inside release_group.

Example:

```
release_group.release_threshold
```

Do not continuously move the threshold unless the strategy explicitly supports dynamic trailing release.

---

# Required Guards

Before evaluating release:

```
phase == SPREAD
```

```
release_group.status == ARMED
```

```
filled_leg is None
```

```
market open
```

---

# Regression Tests

## Test 1

Entry fill creates no release orders.

Expected:

- release_group = ARMED
- zero release orders
- zero pending paper orders

---

## Test 2

Threshold not reached.

Expected:

- no submission

---

## Test 3

Near threshold reached.

Expected:

- exactly one release order submitted

---

## Test 4

Far threshold reached.

Expected:

- exactly one release order submitted

---

## Test 5

Same tick cannot submit both legs.

Expected:

- only one release order exists

---

## Test 6

Reconcile cannot create release orders before trigger.

Expected:

- zero release orders

---

# Acceptance Criteria

Immediately after entry:

```
Dashboard

Entry Orders
✔ visible

Release Orders
✘ none
```

Only after ATR threshold is reached:

```
Dashboard

Release Order
✔ appears
```

The release order must never appear before the trigger condition is satisfied.

---

# Conclusion

Release OCO is a **strategy-level trigger**, not a broker-level resting order.

The correct lifecycle is:

```
ARMED
    ↓
Trigger
    ↓
Submit ONE order
    ↓
Fill
    ↓
Single Leg
    ↓
Trail
```

This architecture eliminates:

- immediate release fills
- executable resting stop orders
- unnecessary sibling cancellation
- premature broker interaction
- several classes of OCO race conditions
