# ADR-0016: MTS Outcome Evaluation Framework, The Five-Tier Evidence Ladder & Dual-Track Governance

**Status**: ACCEPTED  
**Date**: 2026-07-24  
**Author**: Gemini CLI & Quantitative Trading System Architecture Team  
**Scope**: MTS Strategy Analytics, Economic Stability, Dataset Versioning, and Dual-Track Governance  

---

## Context & Problem Statement

Decision Parity (`Wave 1C`/`Wave 1D`) proves system safety (0% logic drift). However, strategy evolution requires proving **Economic Value** and establishing **Evidence Confidence**. 

We establish the **MTS Outcome Evaluation Framework**, **The Five-Tier Evidence Ladder**, and **Dual-Track Governance System** to unify Engineering Safety and Research Science.

---

## The Five-Tier Evidence Ladder

```text
Live Trade
    │
    ▼
Level 1: Execution Quality (System Reliability, Ghost Orders = 0)
    │
    ▼
Level 2: Decision Quality (Context Snapshot, Feature Vector, Parity = 100%)
    │
    ▼
Level 3: Economic Quality (MFE, MAE, PED, Capture Ratio, Stability Distribution)
    │
    ▼
Level 4: Counterfactual Strategy Evolution (Replay across candidate policies)
    │
    ▼
Level 5: Evidence Confidence (Statistical Sample Size, Replay Coverage, Confidence Tag)
    │
    ▼
ADR Registration & Production Deployment
```

### Level 1: Execution Quality
- **Focus**: Network stability, order fill rate, state consistency.
- **KPIs**: Fill rate, reject rate, reconnect count, ghost orders (=0).

### Level 2: Decision Quality
- **Focus**: Context snapshot completeness and decision parity.
- **KPIs**: Parity = 100%, feature vector validity.

### Level 3: Economic Quality & Stability
- **Focus**: Trade profitability, giveback, and distribution stability.
- **Metrics**:
  - **MFE / MAE**: Peak favorable / adverse excursion.
  - **PED (Profit Excursion Decay)**: $MFE - \text{Net PnL}$ (Profit giveback).
  - **Capture Ratio**: $\text{Net PnL} / MFE$ (Retention efficiency, Target > 60%).
  - **Stability Distribution Summary**: Compute `mean`, `median`, `std`, `p10`, `p90`, and `iqr` to detect performance volatility and tail risks.

### Level 4: Counterfactual Strategy Evolution
- **Focus**: Replaying live trade datasets against alternative policies (`PL-01`, `PL-02`, `ATRDynamic`).

### Level 5: Evidence Confidence
- **Focus**: Quantifying statistical significance of research findings based on sample sizes.
- **Confidence Rating Scale**:
  - **`LOW`**: $N < 30$ trade samples (Exploratory hypothesis).
  - **`MEDIUM`**: $30 \le N < 100$ trade samples (Preliminary evidence).
  - **`HIGH`**: $N \ge 100$ trade samples across multiple market regimes (Production Ready).

---

## Versioned Dataset Architecture (`data/trade_dataset/`) & Evidence Freeze

```text
data/trade_dataset/
└── v2026.07.24/                 # Immutable Versioned Dataset
    ├── manifest.json            # SHA-256 Digest & Trade Sample Count
    ├── execution/               # Level 1: Event timeline logs
    ├── decisions/               # Level 2: Decision context snapshots
    ├── outcomes/                # Level 3: Economic quality & stability metrics
    ├── replay/                  # Level 4: Counterfactual replay outputs
    ├── reviews/
    │   ├── trade/               # Per-trade root cause & PED reviews
    │   └── market/              # Market episode & regime shift reviews
    └── research/                # Empirical papers & hypothesis tests
```

### Evidence Freeze Rule
When a research hypothesis (e.g. `R-008`) is completed and registered into an ADR, its underlying dataset directory (`v2026.07.24`) is **FROZEN** with a SHA-256 digest manifest. New live trade data creates a new versioned dataset directory (`v2026.07.25`) to prevent polluting existing evidence.

---

## Evolution Scorecard: Baseline vs Candidates

Baseline is defined as `Baseline = Legacy = Pure Policy` (0% decision drift). Candidates (`PL-01`, `PL-02`) are evaluated against the Baseline:

| Category | Metric | Baseline (Legacy) | Candidate PL-01 | Candidate PL-02 (Target) |
| :--- | :--- | :---: | :---: | :---: |
| **Execution** | Ghost Orders / Latency | 0 / 12ms | 0 / 12ms | 0 / 12ms |
| **Decision** | Decision Parity | 100% | N/A (New Policy) | N/A (New Policy) |
| **Economics** | Avg PED (Giveback) | +2,450 TWD | +1,850 TWD | **+1,220 TWD** |
| **Economics** | Capture Ratio (Mean / Median) | 41% / 38% | 58% / 56% | **71% / 70%** |
| **Economics** | Capture Std Dev | 45% | 28% | **18%** |
| **Confidence**| Sample Count / Level | N/A (Baseline) | N=45 (MEDIUM) | **N=120 (HIGH)** |

---

## Dual-Track Governance System

```text
  Engineering Track                             Research Track
  -----------------                             --------------
        ADR                                       Live Dataset
         │                                             │
         ▼                                             ▼
  Characterization                             Economic Metrics
         │                                             │
         ▼                                             ▼
   Decision Parity                           Counterfactual Replay
         │                                             │
         ▼                                             ▼
    Shadow Soak                                 Hypothesis Test
         │                                             │
         ▼                                             ▼
  Promotion Gate                                Evidence Confidence
         │                                             │
         └──────────────────────┬──────────────────────┘
                                ▼
                       Production Deployment
```

- **Engineering Track**: Answers "Is this change safe and non-breaking?"
- **Research Track**: Answers "Does this change bring proven economic value?"
- Both tracks MUST pass and converge at the **ADR** before Production Deployment.

---

## Consequences

- **Positive**: Unifies engineering rigor and financial science; eliminates unverified parameter tuning; ensures reproducible research with immutable versioned datasets.
- **Negative**: Adds formal dataset versioning overhead.
