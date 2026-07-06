# futures_condor_integrated_v2.md

## Overview

This document defines a **futures-based synthetic iron condor analog** for Taiwan index futures.
It is **not** a true options Iron Condor.

The correct engineering framing is:

> **calendar spread / near-far month mean-reversion strategy**

It uses:

- near-month / far-month futures spread
- VWAP stretch on near month
- spread stretch on near-far basis
- regime filter from the existing futures router
- strict stop-loss and forced exit logic

The strategy is intended for:

- **WEAK / range-like regime**
- low directional persistence
- price oscillation around VWAP
- spread normalization / basis mean reversion

It is **not** intended for TREND regime.

---

## 1. Reality Check

This strategy is only an **analogy** to Iron Condor.

### What it is NOT

- not a true options Iron Condor
- not a theta-decay strategy
- not risk-capped like long-wing options structures
- not safe just because it is "hedged" with far month

### What it IS

- a near/far calendar spread
- a relative-value mean-reversion strategy
- a range-regime drawdown-control strategy
- a research / backtest-first design before live trading

---

## 2. Strategy Objective

The main objective is **not maximum profit**.

The main objective is:

> **reduce drawdown and harvest small reversion gains during WEAK / CHOP-like market conditions**

This strategy should complement trend / momentum strategies by operating only when:

- directional edge is weak
- breakout continuation is less likely
- price and basis are both stretched and likely to normalize

---

## 3. Core Thesis

The strategy assumes:

1. Near-month reacts faster to spot and short-term order flow
2. Far-month usually follows, but less violently
3. When near-month price is stretched relative to VWAP
4. And near-far spread is stretched relative to its own history
5. A reversion opportunity may exist

### Critical Rule

> **Do not trade on VWAP stretch alone.**

VWAP alone is insufficient because a strong trend day can stay above or below VWAP for a long time.

Entry requires BOTH:

- price stretch
- spread stretch

---

## 4. Regime Filter

This strategy should be allowed only in non-trend conditions.

### Allowed Regime

- `WEAK`

### Hard Block

- `TREND`
- `SQUEEZE` (optional block; recommended initially)
- abnormal event / session edge / settlement distortion

### Suggested Existing Feature Filters

```text
regime == WEAK
AND ADX < 25
AND breakout_strength < 0.50
AND volume_spike < 1.2
AND abs(price_vs_vwap) is not already in runaway state
```

### Optional Additional Filters

- VWAP slope near flat
- ATR not rapidly expanding
- no opening drive persistence
- no first-30-minute fresh session entries
- no known macro event window

---

## 5. Instruments and Spread Definition

### Instruments

- near-month futures contract
- far-month futures contract

Examples:
- TX near vs TX next month
- MTX near vs MTX next month

### Spread

```text
spread = near_close - far_close
```

### Derived Metrics

- `vwap_z = (near_close - near_vwap) / near_vwap_std`
- `spread_ma = rolling_mean(spread)`
- `spread_std = rolling_std(spread)`
- `spread_z = (spread - spread_ma) / spread_std`

---

## 6. Entry Logic

The strategy uses two layers:

1. **near-month VWAP stretch**
2. **spread stretch**

Both must agree.

### 6.1 Fade Overheated Upside

Conditions:

```text
vwap_z >= entry_vwap_z
AND spread_z >= entry_spread_z
AND regime == WEAK
AND breakout_strength < threshold
AND ADX < threshold
```

Action:

```text
SELL near month
BUY far month
```

Interpretation:

- near month is rich / overheated
- spread is also stretched high
- fade near-month overextension

---

### 6.2 Fade Over-Sold Downside

Conditions:

```text
vwap_z <= -entry_vwap_z
AND spread_z <= -entry_spread_z
AND regime == WEAK
AND breakout_strength < threshold
AND ADX < threshold
```

Action:

```text
BUY near month
SELL far month
```

Interpretation:

- near month is cheap / over-sold
- spread is also stretched low
- fade downside extension

---

## 7. Recommended Initial Research Thresholds

These are **research starting points**, not production defaults.

```text
entry_vwap_z = 2.0
entry_spread_z = 2.0
take_profit_vwap_z = 0.5
take_profit_vwap_z_extended = 0.3
max_holding_bars = 6 to 12   (for 5m bars)
tp_delay_bars = 1 to 3
```

Do not aggressively optimize before stop-loss framework is stable.

---

## 8. Exit Framework (Most Important Section)

This strategy lives or dies on exit quality.

### Exit Design Principle

Exit logic should be split into two layers:

1. **Hard exit / capital protection**
2. **Soft exit / profit optimization**

The key rule is:

> **Trend exit is never delayed. Only take-profit exit may be optimized with small time or price buffers.**

---

### 8.1 Hard Exit (Not Delayable)

These exits protect against regime failure and must happen immediately.

#### 8.1.1 Regime Exit (Highest Priority)

Exit immediately if market becomes trend-like:

```text
regime == TREND
OR breakout_strength >= 0.60
OR ADX >= 30 with directional persistence
```

This should be a **forced global exit**, not optional strategy logic.

---

#### 8.1.2 VWAP Continuation Exit

Exit if price keeps moving away instead of reverting:

```text
abs(price_vs_vwap) > continuation_threshold
```

Example starting point:

```text
continuation_threshold = 0.004
```

Interpretation:
- the range assumption has failed
- market may be entering continuation mode

---

#### 8.1.3 Spread Stop

This is mandatory.

If spread keeps moving against position beyond a threshold:

```text
adverse_spread_move > 1.0 x spread_atr_proxy
```

Then flatten both legs.

Use spread-volatility stop on the spread series itself, not only near-month ATR.

---

#### 8.1.4 Session / Event Exit

Exit or block around:

- settlement distortion windows
- session open instability
- major macro releases
- low-liquidity session edges

---

### 8.2 Soft Exit (Profit Optimization Allowed)

Soft exit is allowed only after the trade has begun to revert and the market still looks non-trending.

Typical confirmation:

```text
abs(vwap_z) <= 1.0
AND ADX < 25
AND breakout_strength remains below trend threshold
```

At this stage, a small delay or extra price buffer may improve PnL.

---

#### 8.2.1 Time Buffer Exit

When take-profit condition is first reached:

```text
abs(vwap_z) <= take_profit_vwap_z
```

do not necessarily exit immediately.

Instead, allow a small holding buffer:

```text
hold 1 to 3 more bars
```

Purpose:
- capture a bit more reversion
- avoid exiting too early in a still-benign range market

Safety rule:
- apply only if hard-exit conditions remain false

---

#### 8.2.2 Price Buffer Exit

A second profit target can be used instead of immediate exit.

Example:

- primary take-profit zone: `abs(vwap_z) <= 0.5`
- extended take-profit zone: `abs(vwap_z) <= 0.3`

Interpretation:
- first zone = good enough reversion
- second zone = optional extra harvest if market remains stable

This creates a controlled way to seek slightly higher PnL.

---

#### 8.2.3 Peak-PnL Giveback Exit

If using time or price buffer, add a giveback cap.

Example:

```text
if current_pnl < 80% of peak_unrealized_pnl:
    exit
```

Purpose:
- prevent small winners from turning into weak exits
- avoid giving back too much after optimization

---

#### 8.2.4 Partial Exit (Optional Advanced Design)

A two-stage exit may improve stability.

Example:

- 50% exit at `abs(vwap_z) <= 0.5`
- remaining 50% exit at `abs(vwap_z) <= 0.2 to 0.3`

This balances:
- realized gains
- extra reversion capture

This is optional and may be harder to implement for two-leg spread execution.

---

### 8.3 Time Stop

This is still required even if soft-exit logic is used.

This is a mean-reversion trade, not a long-duration carry trade.

If reversion does not begin quickly, edge is weak.

Example:

```text
if hold_bars > max_holding_bars:
    exit
```

Suggested initial research:
- `6 to 12` bars on 5m
- or `30 to 60 minutes`

---

### 8.4 Practical Exit Hierarchy

Recommended evaluation order:

1. Hard trend / continuation exit
2. Spread stop
3. Session / event exit
4. Time stop
5. Soft take-profit optimization
6. Extended take-profit / partial exit

This order ensures that profit optimization never overrides risk control.

---

## 9. Why This Can Still Lose Big

The far-month leg reduces outright risk, but it does **not** hard-cap loss like options wings.

Main failure mode:

- persistent trend day
- spread keeps widening
- near month does not revert
- far leg does not protect enough

So the correct mindset is:

> **risk reduction, not risk elimination**

---

## 10. Execution Reality

This strategy is not a config-only change.

Live trading would require dedicated support for:

1. near/far contract discovery
2. dual-leg data subscription
3. spread position state
4. spread PnL accounting
5. two-leg order submission
6. leg recovery when one leg fills first
7. session / settlement guards

### Critical Live Constraint

Two independent futures orders are **not** a safe atomic combo.

If one leg fills and the hedge leg does not, the account temporarily becomes one-sided.

Any live rollout must include **leg recovery logic**.

---

## 11. Router Integration

This strategy should be added as a dedicated strategy module.

### Proposed Strategy Name

```text
calendar_condor
```

### Initial Router Placement

- regime: `WEAK` only
- priority: **low**
- after primary directional / momentum candidates

Recommended initial position in weak-strategy list:

```text
[counter_vwap, spring_upthrust, kbar_feature, calendar_condor]
```

Reason:
- avoid suppressing stronger single-leg directional signals too early
- let attribution confirm whether this strategy deserves higher priority later

---

## 12. Attribution / Evaluation Goals

This strategy should be evaluated with the same attribution framework:

Track:
- candidate_count
- eval_count
- winner_count
- shadowed_count
- trade_count
- avg_pnl
- drawdown
- regime distribution

### The key question is NOT:

> "Does it make the most money?"

### The key question IS:

> "Does it improve WEAK-regime performance and reduce drawdown?"

---

## 13. Research / Backtest Requirements

Before live deployment, verify:

1. aligned near/far bars
2. realistic fees for both legs
3. slippage for both legs
4. spread-aware stop-loss behavior
5. loss distribution on trend days
6. day vs night session behavior
7. settlement-week distortion
8. no look-ahead in rolling VWAP / spread statistics
9. compare immediate take-profit vs buffered take-profit
10. measure giveback after delayed exits

### Minimum Research Questions

1. How often does spread revert after both `vwap_z` and `spread_z` exceed thresholds?
2. How long does reversion usually take?
3. What is the loss distribution on strong trend days?
4. Does edge survive double-leg fees and slippage?
5. Is night-session behavior materially worse?
6. Should settlement-adjacent days be blocked entirely?
7. Does `tp_delay_bars` improve average PnL without worsening tail loss?
8. Does peak-PnL giveback control improve realized exits?

If these are not answered, the strategy is not ready for live use.

---

## 14. Engineering Design Summary

### Strategy Type

- research / backtest-first
- futures calendar spread analog
- range-only / mean-reversion

### Entry

- WEAK regime only
- VWAP stretch confirmed
- spread stretch confirmed
- no trend continuation evidence

### Exit

- immediate hard exit on trend / continuation
- spread stop
- time stop
- optional soft take-profit optimization
- optional extended TP / partial exit

### Objective

- reduce WEAK-regime drawdown
- add controlled non-trend edge
- complement, not replace, momentum strategies

---

## 15. Final Framing

The correct framing is:

> **This is not an Iron Condor implementation.**
>
> It is a **calendar-spread mean-reversion strategy inspired by the intuition of Iron Condor**:
> profit when the market stays non-directional and stretched conditions normalize.

That framing should guide:
- naming
- backtest expectations
- stop-loss design
- live execution engineering
