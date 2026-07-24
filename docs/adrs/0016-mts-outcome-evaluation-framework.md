# ADR-0016: MTS Outcome Evaluation Framework & The Four-Tier Evidence Ladder

**Status**: ACCEPTED  
**Date**: 2026-07-24  
**Author**: Gemini CLI & Quantitative Trading System Architecture Team  
**Scope**: MTS Strategy Analytics, Economic Quality, & Counterfactual Strategy Evolution  

---

## Context & Problem Statement

Decision Parity (`Wave 1C`/`Wave 1D`) proves that a newly extracted policy does not alter legacy decision logic. However, parity alone does not guarantee trading profitability or optimal execution. 

To bridge the gap between **System Parity** ("did we avoid breaking things?") and **Economic Value** ("are we systematically improving trading performance?"), we establish the **MTS Outcome Evaluation Framework** and **The Four-Tier Evidence Ladder**.

---

## The Four-Tier Evidence Ladder

```text
Live Trade
    │
    ▼
Level 1: Execution Quality (System Reliability & Fill Metrics)
    │
    ▼
Level 2: Decision Quality (Snapshot, Features, Context & Parity)
    │
    ▼
Level 3: Economic Quality (MFE, MAE, PED, Capture Ratio)
    │
    ▼
Level 4: Strategy Evolution & Counterfactual Replay
    │
    ▼
Hypothesis Testing & ADR Registration
    │
    ▼
Production Deployment
```

### Level 1: Execution Quality
- **Focus**: System reliability, network latency, order fill rates, state consistency.
- **KPIs**: Fill rate, reject rate, reconnect count, ghost orders (=0), state sync latency.

### Level 2: Decision Quality
- **Focus**: Decision rationality, context snapshotting, and parity comparison.
- **KPIs**: Decision Parity (100%), context snapshot completeness, feature vector validity.

### Level 3: Economic Quality
- **Focus**: Trade profitability and efficiency metrics.
- **KPIs**:
  - **MFE (Maximum Favorable Excursion)**: Peak unrealized profit.
  - **MAE (Maximum Adverse Excursion)**: Peak unrealized loss.
  - **PED (Profit Excursion Decay)**: $MFE - \text{Net PnL}$ (Profit giveback from peak).
  - **Capture Ratio**: $\text{Net PnL} / MFE$ (Efficiency of profit retention, Target > 60%).
  - **Release Efficiency**: $MFE_{\text{release}} / MFE_{\text{peak}}$.

### Level 4: Strategy Evolution & Counterfactual Replay
- **Focus**: Empirical strategy improvement via counterfactual replay across alternative policies.
- **Methodology**: Replay live trade datasets against alternative exit policies (`ProfitLock`, `ATRDynamic`, `ImmediateExit`), compute delta metrics (Win Rate, Profit Factor, PED Reduction), and record empirical evidence before registering an ADR for production deployment.

---

## Permanent Data Warehouse Architecture (`data/trade_dataset/`)

```text
data/trade_dataset/
├── execution/       # Level 1: Raw event timelines & order fill logs
├── decisions/       # Level 2: Decision snapshots & context feature vectors
├── outcomes/        # Level 3: Economic quality metrics (MFE, MAE, PED, Capture Ratio)
├── replay/          # Level 4: Counterfactual policy replay datasets & diffs
├── manifests/       # Immutable soak and replay manifests
├── reviews/         # Qualitative trade reviews & market regime tags
└── research/        # Empirical papers & hypothesis test evidence
```

---

## Evolution Scorecard

Every strategy release MUST export an Evolution Scorecard comparing performance across versions:

| Metric Category | Metric | Legacy v1.0 | Pure Policy v1.1 | Candidate v1.2 (Target) |
| :--- | :--- | :---: | :---: | :---: |
| **Execution** | Reliability / Ghost Orders | 99.9% / 0 | 100% / 0 | 100% / 0 |
| **Decision** | Decision Parity | 100% | 100% | 100% |
| **Economics** | Avg PED (Giveback) | +2,450 TWD | +2,450 TWD | < 1,200 TWD |
| **Economics** | Capture Ratio ($\text{PnL}/MFE$) | 41% | 41% | > 65% |
| **Economics** | Profit Factor | 1.08 | 1.08 | > 1.35 |

---

## Consequences

- **Positive**: Establishes a scientific, empirical foundation for quantitative strategy evolution. Eliminates "vibe-fixing" or intuition-based parameter tuning.
- **Negative**: Requires persistent storage and computation of MFE/MAE/PED metrics for every completed trade.
