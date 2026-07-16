# Research Template

Use this template for every new research entry.

---

## Header

```
R-NNN: <Short Descriptive Title>
Status: In Progress / Accepted / Rejected / Superseded
Start Date: YYYY-MM-DD
End Date: YYYY-MM-DD (when resolved)
Evidence Level: E0–E5 (per ADR-016)
References: ADR-015, ADR-016, ...
```

---

## Problem

What question is this research trying to answer?

State the hypothesis clearly. Example: "BB position at release time contains predictive information about post-release PnL."

---

## Dataset

| Field | Type | Description |
|-------|------|-------------|
| `trade_id` | str | Unique trade identifier |
| `session` | str | Day / Night |
| `regime` | str | Market regime at release |
| `release_leg` | str | Near / Far |
| `spread` | float | Spread value at release |
| `spread_z` | float | Z-score at release |
| `bb_position` | str | Upper / Middle / Lower / Outside |
| `bb_width` | float | Current BB width |
| `sqz_on` | bool | Squeeze state at release |
| `atr` | float | Current ATR |
| `release_reason` | str | Why release was triggered |
| `confirm_ticks` | int | Ticks before confirmation |
| `confirm_ms` | float | Elapsed ms before confirmation |
| `release_price` | float | Exit price of released leg |
| `MFE_after_release` | float | Max favorable excursion after release |
| `MAE_after_release` | float | Max adverse excursion after release |
| `PnL_after_release` | float | Final PnL contribution |

**Sample size target:** Minimum 100, preferred 300+. Continue until confidence interval stabilizes.

---

## Method

1. Collect data from shadow telemetry (no decision impact)
2. For each release, record BB position and post-release outcome
3. Group by BB position bucket (Upper / Middle / Lower / Outside)
4. Compare MFE, MAE, PnL across buckets
5. Report per-bucket: mean, median, std, 25th/75th percentiles, n count
6. Regime- and session-breakdown if sample permits

---

## Counterfactual Analysis

If BB had been used to [adjust release / tighten trail / block release], how many decisions would have changed? Would the outcome have been better or worse?

Required metrics:
- Decision-change count
- Win-rate delta (with confidence interval)
- PnL delta per trade and cumulative
- MFE/MAE comparison

---

## Out-of-Sample Validation

Apply counterfactual to a non-overlapping time period. Report:
- In-sample vs OOS performance
- Overfitting assessment (gap between IS and OOS)

---

## Result

### Accepted

The hypothesis is supported. State the key numbers and conclusion.

### Rejected

The hypothesis is not supported. State what was tested and what was found. Negative results are permanent assets — they prevent re-proposal.

---

## Decision

- **Evidence Level reached**: E?
- **Recommended next step**: [None / Shadow Assist / ADR proposal / Further research]
- **Impact on strategy**: [None / Parameter change / New indicator / Removal]

---

## Failure Modes

Any known limitations, edge cases, or caveats:
- Regime-specific breakdown
- Roll-window sensitivity
- Sample size constraints
- Data quality issues
