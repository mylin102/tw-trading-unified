# R-001: BB Position at Release

**Status:** In Progress
**Start Date:** 2026-07-16
**Evidence Level:** E1 (Shadow)
**References:** ADR-014, ADR-015, ADR-016

---

## Problem

Does BB position at release time contain predictive information about post-release PnL?

More precisely: when a release occurs with the released leg's price at different BB positions (Upper, Middle, Lower, Outside), is there a statistically significant difference in subsequent MFE, MAE, or final PnL?

If yes, BB position could inform:
- Profit lock tightening on the remaining leg
- Trail adjustment after release
- Entry confirmation (BB + Z-score)

If no, BB should remain as shadow telemetry and not enter the decision engine.

---

## Hypothesis

**H0:** The distribution of post-release PnL is independent of BB position at release time.

**H1:** Releases occurring at Lower BB are followed by more favorable outcomes (higher MFE, lower MAE, higher final PnL) than releases at Upper BB or Middle.

(Rationale: In a mean-reverting calendar spread, extreme BB positions suggest the spread is near a statistical extreme, making reversion more likely.)

---

## Dataset Schema

| Field | Source | Notes |
|-------|--------|-------|
| `trade_id` | `mts_trade_fills.jsonl` | Unique trade identifier |
| `session` | From bar timestamp | Day / Night |
| `regime` | From bar | Market regime classifier |
| `release_leg` | Release decision | Near / Far |
| `spread` | `bar["spread"]` | Near - Far at release bar |
| `spread_z` | `bar["spread_z"]` | Rolling Z-score at release |
| `bb_position` | Computed from `bb_upper/lower` vs `release_price` | Upper / Middle / Lower / Outside |
| `bb_width` | `bb_upper - bb_lower` | Current BB width |
| `sqz_on` | `bar["sqz_on"]` | Squeeze state at release |
| `atr` | `bar["atr"]` | Current ATR |
| `release_reason` | Release decision | e.g. threshold, emergency |
| `confirm_ticks` | Release chain | Ticks before confirmation |
| `confirm_ms` | Release chain | Elapsed ms before confirmation |
| `release_price` | Released leg price | Exit price |
| `MFE_after_release` | Tick data after release until trade end | Max favorable excursion |
| `MAE_after_release` | Tick data after release until trade end | Max adverse excursion |
| `PnL_after_release` | Final PnL of the loop | Net profit including remaining leg |

---

## Method

### Stage 1: Data Collection (Shadow — no decision impact)

The `[MTS_RELEASE_SHADOW]` log already captures `sqz_on`, `near_bb_upper/lower`, `far_bb_upper/lower` at each release. The release telemetry log captures PnL, ticks, and timing.

Pipeline:
1. Parse `[MTS_RELEASE_SHADOW]` log lines → extract BB position per release
2. Parse `[MTS_RELEASE_EVAL]` log lines → extract PnL, ticks, timing
3. Parse `mts_trade_fills.jsonl` → extract final loop PnL
4. Join on `trade_id`
5. Compute `bb_position` bucket: if price ≈ bb_upper → Upper, ≈ bb_lower → Lower, between → Middle, outside → Outside

### Stage 2: Counterfactual Analysis

Once >= 100 samples collected:
1. Group by `bb_position` bucket
2. Report per-bucket statistics: mean/median/std/p25/p75 of MFE, MAE, PnL
3. Compare across buckets with non-parametric test (Mann-Whitney U)
4. If signal found, estimate: how many decisions would have changed if BB were used?

### Stage 3: Out-of-Sample Validation

Split data chronologically: first 70% in-sample, last 30% out-of-sample.
If signal holds in both slices, proceed to Stage 4.

### Stage 4: Production Shadow Assist

Display BB position on dashboard as operator reference.
No autonomous decision engine impact.
Collect operator feedback on whether BB position aligns with intuitive expectations.

---

## Sample Size Target

| Threshold | Condition |
|-----------|-----------|
| Minimum | 100 releases |
| Preferred | 300+ releases |
| Continue until | Confidence interval for PnL difference between BB buckets stabilizes (width does not shrink by >10% per 50 additional samples) |

---

## Current Status

Shadow telemetry active since ADR-014 Phase 1 deployment (commit `fba77125`).
`[MTS_RELEASE_SHADOW]` logging all BB positions at each release.
No counterfactual analysis started — insufficient samples.

---

## Failure Modes

- **Uneven bucket distribution:** Most releases may cluster at one BB position (e.g., Middle), making cross-bucket comparison underpowered.
- **Regime confounding:** If regime dominates the effect (e.g., all releases in Chop are at Middle BB), the BB position signal may be a regime proxy.
- **Roll-window contamination:** During contract rollover, BB values may be structurally different.
- **Sample not independent:** Multiple releases from the same trade loop share a trade_id; mixed-effects model may be needed.
- **Survivorship bias:** Only completed trades are analyzed. Open trades at end of sample are excluded.

---

## References

- ADR-014: Squeeze/BB release gate removed
- ADR-015: Statistical Arbitrage Framework
- ADR-016: Evidence-Based Indicator Lifecycle
- ADR-009: Position Lifecycle OCA
