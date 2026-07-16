# ADR-016 — Evidence-Based Indicator Lifecycle

**Status:** Proposed
**Date:** 2026-07-16
**Supersedes:** N/A — companion to ADR-015

> **Absence of evidence is not evidence of absence. However, absence of sufficient evidence is sufficient reason to exclude an indicator from the Decision Engine.**

---

## Background

ADR-015 established that MTS Calendar Spread is a Statistical Arbitrage strategy, not Trend Following. That answered: **what should we believe?**

It did not answer: **what level of evidence is required before we act on that belief?**

Without an evidence framework, every new indicator is debated case-by-case. The same debates recur (Squeeze, BB, Half-life, OU). Some indicators enter the Decision Engine prematurely. Others are repeatedly re-proposed because no negative result was archived.

ADR-016 addresses this by defining a universal **Evidence-Based Indicator Lifecycle** that every indicator must follow — from Idea to Retirement.

---

## Relationship to ADR-015

| ADR | Question |
|-----|----------|
| ADR-015 | What should we believe? (theoretical framework) |
| ADR-016 | What evidence is required before we act on that belief? (evidence framework) |

Both are intentionally stable framework documents. Specific indicators (Half-life, Structural Break Detection, etc.) should be proposed in subsequent ADRs that reference both of these documents, rather than by expanding either one.

---

## Indicator Lifecycle

```
Idea
  │
  ▼
Shadow Collection
  │
  ▼
Counterfactual Analysis
  │
  ▼
Offline Backtest
  │
  ▼
Out-of-Sample Validation
  │
  ▼
Production (Shadow Assist)
  │
  ▼
Decision Engine
  │
  ▼
Continuous Monitoring
  │
  ▼
Retirement
```

### Stage 1: Idea
A hypothesis about an indicator's predictive value. No code required. Documented for traceability.

**Evidence Level:** E0

### Stage 2: Shadow Collection
The indicator is computed alongside existing pipeline but never consulted for decisions. Data is collected for later analysis. This stage has zero risk.

**Evidence Level:** E1

*Example: BB position at release time logged as shadow telemetry.*

### Stage 3: Counterfactual Analysis
Using historical data: "Given the decisions the system actually made, would this indicator have improved them?"

Questions to answer:
- How many times would the indicator have changed the decision?
- In those cases, was the outcome better or worse?
- What is the win-rate delta?
- What is the PnL delta (per trade and cumulative)?
- MFE/MAE comparison with and without the indicator?
- Breakdown by regime, session, contract period?

**Evidence Level:** E2

### Stage 4: Offline Backtest
A full backtest with the indicator integrated into the decision engine, using historical data.

Requirements:
- Realistic slippage and commission assumptions
- At minimum X trades (defined per indicator type)
- Results compared against baseline (no indicator)
- Breakdown by: regime, session, roll-window vs normal, contract period

**Evidence Level:** E3

### Stage 5: Out-of-Sample Validation
Backtest on data not used during the Counterfactual or Offline Backtest stages.

Requirements:
- Non-overlapping time period
- If walk-forward: sufficient folds for statistical power
- Results compared against in-sample performance
- Overfitting check: in-sample vs out-of-sample performance gap

**Evidence Level:** E4

### Stage 6: Production (Shadow Assist)
The indicator runs in production, displayed on the dashboard as a reference but still not controlling the Decision Engine.

Purpose:
- Validate that real-time behavior matches backtest expectations
- Catch edge cases not present in historical data
- Build operator confidence before autonomous use

**Duration:** At least N trading days (defined per indicator type)

**Evidence Level:** E5

### Stage 7: Decision Engine
The indicator becomes an input to the Decision Engine — but always subject to the invariant from ADR-014/015: **indicators may influence entry and risk calibration; indicators must not veto a confirmed protective exit.**

**Evidence Level:** E6

### Stage 8: Continuous Monitoring
Once in the Decision Engine, the indicator's contribution is continuously tracked.

Metrics to monitor:
- Decision-rate when indicator fires vs doesn't
- PnL contribution decomposition
- Regime-specific performance (did it drift?)
- Roll-window performance
- Degradation over time

If the indicator's contribution drops below a threshold, it may be downgraded to Shadow Assist or Retired.

**Evidence Level:** E6 (active monitoring)

### Stage 9: Retirement
The indicator is removed from the Decision Engine (and possibly from computation entirely).

Reasons:
- Statistically validated degradation
- Regime change makes it structurally irrelevant
- Replacement by a superior indicator
- Cost of computation exceeds marginal benefit

Retired indicators are NOT deleted. They are archived with their evidence history (including negative results) to prevent re-proposal.

**Evidence Level:** E7

---

## Evidence Levels Summary

| Level | Stage | Meaning |
|-------|-------|---------|
| E0 | Idea | Hypothesis only, no data |
| E1 | Shadow | Telemetry collected, no decision impact |
| E2 | Counterfactual | Historical improvement suggested |
| E3 | Offline Backtest | Backtest positive |
| E4 | OOS Validation | Out-of-sample confirmed |
| E5 | Shadow Assist | Production shadow, operator visible |
| E6 | Decision Engine | Actively controlling decisions |
| E6+ | Monitoring | Performance tracked for degradation |
| E7 | Deprecated / Retired | Archived with evidence history |

---

## Current Indicator Evidence Levels

| Indicator | Level | Status |
|-----------|-------|--------|
| Z-score | E6 | Primary entry — core assumption, always active |
| EMA20/EMA60 | E6 | Trend filter |
| ATR | E6 | Dynamic stop, trail distance |
| VWAP | E6 | Exit management, single-leg execution |
| Profit Lock | E6 | Profit protection |
| Release Stop (fixed/ATR) | E6 | Risk control |
| Tick/time confirmation | E6 | Execution integrity |
| Quote age guard | E6 | Data freshness |
| BB (price position) | E1 | Shadow telemetry — Candidate for E2 research |
| Squeeze (sqz_on) | E1 | Shadow telemetry — removed from release gate per ADR-014 |
| Emergency bypass | E6 | Quote guard bypass — kept as risk escalation |
| Spread Distribution Model | E0 | Idea — future ADR candidate |
| Half-life | E0 | Idea — requires measurement before actionability |
| Structural Break Detection | E0 | Idea — future ADR candidate |
| OU Process | E0 | Idea — depends on half-life measurement |
| Kalman Filter | E0 | Idea — no evidence yet |
| Order Flow / DOM | E0 | Idea — no evidence yet |

---

## Acceptance Criteria for Stage Advancement

### E1 → E2 (Shadow → Counterfactual)
- Minimum N observations collected (specified per indicator — e.g., 200 releases for BB position analysis)
- Data spans at least N distinct trading days

### E2 → E3 (Counterfactual → Offline Backtest)
- Counterfactual win-rate delta > 0 with 95% confidence interval excluding zero
- Counterfactual PnL delta positive (per-trade and cumulative)
- Economic significance: improvement exceeds increased friction cost (if any)
- Improvement is not concentrated in a single regime or session

### E3 → E4 (Offline Backtest → OOS Validation)
- Offline backtest meets all acceptance criteria
- No obvious overfitting indicators (excessive parameter count, fragile thresholds)

### E4 → E5 (OOS Validation → Production Shadow)
- OOS results are consistent with in-sample results (not significantly worse)
- Performance gap between in-sample and OOS is acceptable (defined per indicator type)

### E5 → E6 (Shadow Assist → Decision Engine)
- Minimum N trading days in production shadow with consistent behavior
- No unanticipated negative edge cases discovered
- Operator confidence established
- Monitoring metrics defined and operational

### E6 → E7 (Decision Engine → Retirement)
- Continuous monitoring shows performance degradation below threshold
- Or: structural regime change invalidates the indicator's premise
- Or: replacement indicator is at E6 with superior performance

---

## Negative Results Repository

Every indicator that reaches at least E1 (Shadow) should have its evidence history preserved — including negative results.

### Purpose
- Prevent repeated re-proposal of indicators already ruled out
- Provide traceability for why an indicator was not advanced
- Enable meta-analysis: which types of indicators tend to fail at which stages?

### Required fields
- Indicator name and type
- Highest evidence level reached
- Date range of analysis
- Sample size
- Key metrics (PnL delta, win-rate delta, MFE/MAE)
- Reason for stopping (or current level if active)
- Link to analysis artifacts (if any)

### Example entries

```
Indicator: Squeeze (sqz_on) as Release gate
Highest Level: E2 (Counterfactual)
Analysis date: 2026-07-16
Sample size: N releases
Result: No evidence that Squeeze ON/OFF predicts release outcome.
  PnL delta when gating by Squeeze: -X%
Reason stopped: Removed per ADR-014. Replaced by threshold+confirmation baseline.
```

```
Indicator: BB position at release time
Highest Level: E1 (Shadow)
Analysis date: 2026-07-16
Sample size: Telemetry collecting
Result: Pending — insufficient data.
Reason stopped: Awaiting sufficient sample for E2 analysis.
```

---

## Core Principles

1. **Risk management takes precedence over indicator optimization.** An indicator that improves PnL but weakens risk control is rejected.
2. **Statistical and economic significance are both required.** A +0.3% PnL gain with +200% turnover increase is rejected.
3. **Confidence intervals matter.** A point estimate without uncertainty bounds is insufficient.
4. **Regime-specific performance must be reported.** An indicator that works in Chop but loses in Trend is not a general improvement until validated.
5. **Roll-window performance must be reported separately.** Mechanical microstructure changes can create spurious correlations.
6. **Negative results are preserved, not deleted.** They prevent re-proposal and build institutional knowledge.
7. **An indicator's lifecycle never ends at E6.** Continuous monitoring may downgrade it back to E5 or retire it at E7.

---

## Relationship to Future ADRs

Subsequent ADRs (e.g., Half-life Estimation, Structural Break Detection) should:
- State the current evidence level of the indicator
- Propose the next stage advancement
- Reference ADR-015 for theoretical foundation
- Reference ADR-016 for evidence requirements

This prevents each new ADR from re-arguing the evidence methodology and keeps the focus on the indicator-specific analysis.

---

## Migration Path

Existing indicators at E6 (Z-score, ATR, VWAP, Profit Lock, Release Stop) are grandfathered in — they do not need to go through the lifecycle from the beginning. However, any proposed modification to how they operate should include a counterfactual analysis comparing old vs new behavior.

New indicators (anything at E0 or E1) must follow the full lifecycle.

---

## Out of Scope

This ADR does NOT specify:
- The exact statistical test to use at each stage (stage-specific decision criteria belong in subsequent ADRs or a living Evidence Handbook)
- The minimum sample size — this varies by indicator type and should be defined per proposal
- The calculation methodology for any specific indicator
- The dashboard or tooling implementation for the Negative Results Repository
- How Continuous Monitoring metrics are surfaced (operator dashboards, automated alerts)

These implementation details should be decided when the first indicator reaches the relevant stage.

---

## Conclusion

ADR-016 formalizes a question that every indicator must answer before entering the Decision Engine:

> **What level of evidence justifies acting on this belief?**

By defining a universal lifecycle with clear stage gates, evidence levels, and a negative results repository, it prevents premature adoption, reduces recurring debates, and ensures the system evolves conservatively — consistent with ADR-015's statistical arbitrage framework and the principle that risk management takes precedence over optimization.

---

## Future Revisions (Not Blocking Current Commit)

Three refinements identified during design review, deferred to a future revision:

### 1. Exit Criteria per Stage

Each stage should define both a Promotion Gate (what qualifies to advance) and an Exit Criteria (what triggers retreat). Tentative mapping:

| Transition | Promotion Gate | Exit Criteria |
|------------|---------------|---------------|
| E1 → E2 | Shadow data sufficient | Telemetry quality insufficient |
| E2 → E3 | Counterfactual positive | No statistical improvement |
| E3 → E4 | OOS passes | OOS collapses |
| E4 → E5 | Shadow Assist stable | Live divergence |
| E5 → E6 | Extended stability | Performance drift |
| E6 → E7 | Sustained degradation | Retirement triggered |

### 2. Evidence Contract (Standardized Indicator ADR Template)

Every indicator ADR should include a fixed-format Evidence Report:

```
Evidence Report
- Sample Size
- Shadow Duration
- Counterfactual Result
- OOS Result
- Confidence Interval
- Economic Significance
- Failure Modes
- Negative Findings
```

This ensures comparability across indicators and prevents selective reporting.

### 3. Decision Budget

Indicators must justify incremental complexity, not just marginal PnL:

> Every indicator must justify its incremental complexity.

A +0.2% PnL improvement that adds 300 lines of code, 5 config parameters, and 3 test modules may be rejected on engineering grounds alone — even if statistically significant.

### 4. Research Directory Structure

Negative results should be stored in a standardized directory:

```
docs/research/
  accepted/
  rejected/
  superseded/
```

This gives every research attempt (including failures) a permanent home and prevents re-proposal cycles.
