# ADR-015 — Reframe MTS Calendar Spread as Statistical Arbitrage

**Status:** Proposed — Intentionally Stable
**Date:** 2026-07-16
**Design Review:** 2026-07-16 (6 refinements incorporated)

> ADR-015 is an intentionally stable framework document. New indicators or statistical models should be introduced through subsequent ADRs that reference this document, rather than by expanding it further.

---

## Background

Recent refactoring removed the dependency between MTS Release and TTM Squeeze (ADR-014).

During the design review an important architectural observation emerged:

> Calendar Spread is fundamentally a **Statistical Arbitrage / Mean Reversion** strategy, not a Trend Following strategy.

This changes how technical indicators should be evaluated and which research literature is relevant.

---

## Core Principle

### Current spread definition

```
Spread = Near Contract - Far Contract
```

### Trading process

```
Spread deviates
        ↓
Z-score exceeds threshold
        ↓
Enter spread
        ↓
Wait for mean reversion
        ↓
Exit
```

This is conceptually related to FX Pair Trading systems — but with critical structural differences (see §Contract Lifecycle and §Market Microstructure).

It is NOT equivalent to momentum breakout trading.

---

## Critical Refinement: Mean is LOCAL, not Global

Calendar Spread does not revert to a fixed global mean. The mean itself drifts across sessions and contract regimes:

```
Session A       Session B       Session C
Mean = -210     Mean = -235     Mean = -180
    │               │               │
    └───────┬───────┘               │
            │                       │
       Regime Shift             Rollover
```

What reverts is the **Local Mean**, not the Global Mean.

### Invariant

```
Statistical assumptions are local.
Never assume stationarity across
contract rollovers or regime shifts.
```

This has direct consequences:
- Z-score must use a rolling window, not a fixed level
- Model calibration should be re-estimated per contract regime
- Half-life estimates are regime-specific, not universal

---

## Trend Following vs Statistical Arbitrage

### Trend Following

Examples: Stocks, Futures breakout, TTM Squeeze, MACD, Donchian, Momentum

Assumption: `Price moving → Price continues moving`

Preferred indicators: Squeeze, ADX, MACD, Momentum, Volume expansion

### Statistical Arbitrage

Examples: FX Arbitrage, Pair Trading, Calendar Spread, ETF Premium Arbitrage

Assumption: `Relative price deviates → Relative price eventually reverts`

Preferred indicators: Spread, Rolling Mean, Rolling Std, Z-score, Bollinger Bands, Structural Break Detection, Half-life

---

## Why Squeeze Does Not Fit Release Logic

Squeeze measures **Volatility Compression**. It does NOT measure:
- Mean reversion probability
- Relative mispricing
- Spread equilibrium
- Release timing

**Current evidence does not support using Squeeze as a Release gate.** Release is a risk-control decision. Squeeze is a volatility-state indicator.

This does NOT rule out future research on Squeeze for parameter calibration:
- Squeeze ON → half-life changes?
- Squeeze ON → spread volatility shrinks → need wider entry?
- Squeeze ON → ATR multiplier adjustment?

But Squeeze must never veto a confirmed protective exit.

---

## BB and Squeeze are Different

These concepts were previously conflated.

### Bollinger Band
Measures **Price Position**. Questions answered: near upper band? near lower band? outside bands?
Used by: Mean Reversion, Profit Taking, Entry Confirmation

### Squeeze
Measures **Volatility Regime**. Questions answered: is volatility compressed? Is BB inside KC?
Used by: Breakout systems, Trend systems.
NOT: Stop-loss gating

---

## Architectural Mapping

| Indicator | Measures | Suitable Usage |
|-----------|----------|----------------|
| Z-score | Statistical deviation | Primary entry |
| EMA20/EMA60 | Trend of spread | Trend filter |
| ATR | Risk | Dynamic stop |
| VWAP | Execution quality | Exit management |
| Profit Lock | Risk | Profit protection |
| BB | Statistical location | Candidate confirmation (shadow) |
| Squeeze | Volatility regime | Optional market context |

---

## Release Decision Pipeline

```
Loss threshold crossed
        ↓
Quote validity
        ↓
Tick deduplication
        ↓
Tick confirmation
        ↓
Time confirmation
        ↓
Lifecycle validity
        ↓
Pending order guard
        ↓
Submit Release
```

No BB gate. No Squeeze gate.

---

## Contract Lifecycle (The Key Difference From FX)

FX Pair Trading has no expiration. Calendar Spread always involves rolling contracts.

This is the single biggest structural difference between FX Stat Arb and Calendar Spread Stat Arb.

### Critical events

1. **First Notice Day** — physical delivery risk changes spread dynamics
2. **Last Trade Day** — volume collapses for expiring contract
3. **Open Interest Migration** — OI shifts from front to next month
4. **Volume Migration** — trading activity shifts to next contract
5. **Roll Window** — mechanical spread compression as market rolls
6. **Settlement** — final price convergence

### Impact on statistical model

During the roll window:
- Spread distribution changes mechanically (not due to mispricing)
- Volume shifts create artificial spread movements
- Mean reversion assumption may temporarily break

### Recommendation

Statistical model calibration should:
- Flag roll window periods
- Exclude or downweight roll window data from mean/std estimation
- Re-estimate local mean after roll completes

---

## Market Microstructure (Layer 1)

Calendar Spread is not only statistics. It is also exchange mechanics.

### Factors that move the spread

- Tick size constraints
- Queue priority at best bid/offer
- Near vs far contract volume disparity
- Bid/ask imbalance per leg
- Open Interest concentration
- Settlement mechanics

Many spread movements attributed to "mean reversion" are actually microstructure effects.

### Three-layer architecture

```
Layer 1: Market Structure
    - Tick size, queue, OI, volume, settlement
    ↓
Layer 2: Statistical Model
    - Rolling mean/std, Z-score, break detection
    ↓
Layer 3: Execution
    - VWAP, ATR, Profit Lock, confirmation
```

Future research must account for Layer 1 before attributing patterns to Layer 2.

---

## Relationship to FX Statistical Arbitrage

MTS Calendar Spread resembles FX Pair Trading in its statistical layer, but differs critically in contract lifecycle and market microstructure.

| FX Stat Arb | Calendar Spread |
|-------------|-----------------|
| Pair Spread | Near-Far Spread |
| Rolling Mean | Spread Mean (local, not global) |
| Rolling Std | Spread Std |
| Z-score | Z-score |
| Mean Reversion | Mean Reversion (local) |
| No expiration | Rolling contracts |

Future research should prioritize Statistical Arbitrage literature over Trend Following literature — but must account for the structural differences that FX Pair Trading does not have.

---

## Potential Future Research

### Half-life

Estimate how long spread requires to revert within a contract regime.

**Caveat:** If half-life = 50 bars but strategy hold time = 3 bars, it is not actionable.

**Research question:** Measure actual half-life in current data. Is it within a range that can inform dynamic timeout, time stop, or profit lock timing?

If not actionable, do not implement.

### Structural Break Detection (not Cointegration Monitoring)

Near/Far contracts are almost always cointegrated by construction (same underlying). Johansen tests add little value.

Instead, monitor **residual changes** for:
- Sudden residual shift (liquidity migration)
- Roll window onset
- Extreme market events
- First notice / last trade proximity

**Renamed from:** Cointegration Monitoring → Structural Break Detection

### OU Process

Estimate mean reversion speed as a potential replacement for fixed confirmation timing.

Requires half-life measurement first.

### Squeeze — Parameter Calibration (Not Gate)

Squeeze may still inform:
- Whether half-life changes under compression
- Whether spread volatility shrinks (needing wider entry)
- Whether ATR multiplier should be Squeeze-dependent

But Squeeze must never veto Release.

### Counterfactual BB Analysis

Collect per-release:
- BB position at release time (upper/middle/lower/outside)
- Future MFE, MAE, final PnL

Example output: `Release @ Lower BB → Average MFE +38, Average MAE -6`

This determines whether BB contains predictive information worth operationalizing.

---

## Design Principles

1. **Indicators may strengthen confidence; indicators must not veto confirmed protective exits.**
2. Risk management takes precedence over market prediction.
3. Shadow metrics first. Production logic later. Every new indicator should first be collected as telemetry and validated by counterfactual analysis before entering the decision engine.

---

## Non-goals

This ADR does NOT assert:

- Calendar spread is always stationary.
- Mean reversion is guaranteed.
- FX statistical arbitrage techniques can be directly applied.
- Every statistical indicator has predictive value in this context.
- The indicators listed in "Potential Future Research" will be implemented.

**This ADR defines a framework for evaluating indicators under a statistical-arbitrage paradigm.** It is not a feature specification. Future ADRs (e.g., Spread Distribution Model, Adaptive Entry Threshold, Half-life Estimation, Structural Break Detection) should reference ADR-015 for the theoretical foundation rather than re-arguing the mean-reversion assumption.

---

## Central Conclusion

> **Calendar Spread should be modeled as a locally mean-reverting statistical process constrained by futures market microstructure.**

Four critical qualifiers:
1. **Locally** — not global mean reversion; per regime, per contract cycle
2. **Mean-reverting** — an assumption, not a guarantee; must be re-validated
3. **Statistical process** — decisions driven by statistical features, not trend indicators
4. **Constrained by futures market microstructure** — contract lifecycle, rollover, settlement, and exchange mechanics prevent direct FX Pair Trading model transfer

---

## Implementation Plan

### Phase 1 (Complete)
- Remove Squeeze gate from Release
- Remove BB gate from Release
- Keep BB telemetry
- Keep Squeeze telemetry
- No replacement logic

### Future Research (not committed implementation)
- Measure half-life in current data → determine actionability
- Build Structural Break Detection (residual monitoring)
- Counterfactual BB analysis
- Squeeze-parameter correlation study
- Contract lifecycle / roll window detection
- Evaluate OU Process as confirmation replacement
