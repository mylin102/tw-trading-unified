# Policy K: Combined-First Adaptive Exit (Trend-Conditional Residual Leg Hold)
<!-- Architectural Specification and 6-Stage Research Roadmap -->

## Executive Summary
This document defines the formal architecture and research roadmap for Policy K.
Policy K establishes Combined Exit as the default risk-off baseline for all Release events, while framing single-leg residual holds as evidence-backed exceptions optimized directly against decision-aligned counterfactual uplift (Y_i).

---

## 1. Core Architecture Principles

1. **Default = Risk Off (Combined Exit)**
   - All Release events default to immediate dual-leg exit (Combined Exit @ Release).
2. **Exception = Evidence-Based Continuation**
   - Residual leg holds are only permitted when empirical features predict positive decision-aligned uplift (Y_i > 0).
3. **Decision-Aligned Target Label (Y_i)**
   Y_i = PnL(SingleLeg, i) - PnL(Combined@Release, i)
   - Targets the exact net economic decision value rather than abstract market trend.
4. **Strata Governance**
   - **Production**: FAR Release (leaving Near) is strictly prohibited (allow_trail_near = False).
   - **Shadow Replay**: FAR Release is fully retained in shadow logging to preserve empirical research coverage.

---

## 2. Six-Stage Policy K Research Roadmap



---

## 3. Decision Profile Metrics

Instead of hardcoding a fixed 70% Precision cutoff, Phase K3 Shadow evaluation will report the complete decision profile:
- **Precision**: P(Y > 0 | GatePassed)
- **Recall**: P(GatePassed | Y > 0)
- **Expected Value / Mean Uplift**: E[Y | GatePassed]
- **Median Uplift**: Median(Y | GatePassed)
- **Worst Decile Impact**: 10th percentile of Y when Gate passed
- **Sample Coverage**: % of total Release events granted exception
