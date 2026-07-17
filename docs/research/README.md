## Research Registry Overview

> **Research ends with an evidence report, not with a production deployment.**

This directory contains past experiments, telemetry analysis, backtests, and negative results for the Taiwan Trading Unified system. Every research candidate must comply with the validation gates of [ADR-016](file:///Users/mylin/Documents/mylin102/tw-trading-unified/docs/adrs/ADR-016-evidence-based-indicator-lifecycle.md).

> The full research methodology — including Replay Taxonomy, Counterfactual Identifiability, Evidence Sufficiency, and Evidence Boundary — is defined in [RESEARCH_METHODOLOGY.md](RESEARCH_METHODOLOGY.md). All R-NNN reports cite this document rather than redefining core concepts.

---

## 1. Defining Research Success

A research project is successful if it produces **reproducible evidence**, regardless of whether the final decision is adoption (`Accepted`) or rejection (`Rejected` / Retired to E7). Rejection is a formal research asset that builds institutional knowledge and prevents repeating failed cycles.

### Lifecycle of a Research Project (R-NNN)
```
E0 Idea → E1 Shadow Telemetry → E2 Counterfactual Analysis → E3 Offline Validation → E4 OOS Validation → Evidence Report & Decision
```

---

## 2. Replay & Counterfactual Methodology

To evaluate candidate indicators and strategy modifications objectively, the research registry adheres to a structured, five-layer simulation methodology applicable to any event-driven Finite State Machine (FSM) system.

### A. Methodology Pipeline

```
Research Question ──► State Coverage ──► Evidence Granularity ──► Evidence Sufficiency ──► Replay Model ──► Counterfactual Capability ──► Experiment Design ──► Evidence Report
```

```
           MTS Research Methodology Architecture

                    Research Question
                            │
                            ▼
     Evidence Model (Granularity, Sufficiency, Coverage)
                            │
                            ▼
       Replay Model (Point Replay, Trajectory Replay)
                            │
                            ▼
   Counterfactual Capability (Decision, Decision Timing)
                            │
                            ▼
                    Experiment Design
```

---

### B. Evidence Model

The Evidence Model defines the data properties required to represent historical operations and validate alternative scenarios.

#### I. Evidence Granularity
Research data is classified into three tiers of granularity:

| Granularity | Definition | Generic Example |
|---|---|---|
| **Snapshot** | Single-point pre-decision state | System variables at evaluation time |
| **Event** | Discrete state transitions | Signal crossings, timer starts, timer resets |
| **Trajectory** | Continuous path series | Historical tick sequence, FSM evolution |

#### II. Evidence Sufficiency (Necessary but Not Sufficient)
Evidence granularity is a necessary condition for replay, but it is not sufficient on its own. The dataset must satisfy **Evidence Sufficiency** invariants to support higher-level simulation models. If any invariant is violated, the data degrades to a *fragment* and cannot support trajectory-level counterfactuals.

| Granularity | Sufficiency Invariants Met? | Replay Capability |
|---|---|---|
| **Snapshot** | Yes (Completeness at point) | **Point Replay** (Decision Counterfactual) |
| **Trajectory** | No (Missing ticks or event gap) | **Unreplayable** (Trajectory Fragment) |
| **Trajectory** | Yes (Completeness, Continuity, Sync) | **Trajectory Replay** (Full Counterfactual) |

* *Example of Insufficiency:* A tick sequence (**Trajectory** granularity) that is missing FSM state transition markers (**Event** granularity) is insufficient to evaluate temporal triggers. It remains an unreplayable trajectory fragment.

#### III. State Coverage (Hypothesis Coverage)
State Coverage consists of the orthogonal dimensions necessary to evaluate the hypothesis under study, rather than simply representing what is available in the dataset. Common dimensions include:
* Market state (trend, mean-reversion, regime shifts)
* Volatility regime (high vs. low compression)
* Temporal context (sessions, time-of-day)
* Structural state (liquidity, contract lifecycle phases)
* Signal properties (long vs. short polarity)

---

### C. Replay Model

The Replay Model defines the simulation engine's execution scope.

* **Point Replay**: Replays a single historical snapshot to verify if a decision changes under modified parameters.
* **Trajectory Replay**: Replays the entire trade path (tick-by-tick or bar-by-bar) to capture *when* decisions trigger.
* **Superset Invariant**: Trajectory Replay is a **strict superset** of Point Replay. It reconstructs stateful FSM transitions over time, enabling both binary decision counterfactuals and timing/slippage counterfactuals.

---

### D. Counterfactual Capability & Identifiability

Counterfactual evaluation is strictly bounded by the replay model used. 

* **Point Replay** can identify a **Decision Counterfactual** (*"Does the decision change at this specific historical snapshot?"*).
* **Trajectory Replay** is required to identify a **Decision Timing Counterfactual** (*"When does the decision trigger over the course of the trajectory?"*).

Tighter threshold evaluations or temporal confirmation gates (such as confirmation tick count or confirmation timer resets) are stateful FSM properties and require Trajectory Replay to remain identifiable.

---

### E. Experiment Design

Experiments must employ **Adaptive Boundary Sampling** rather than static sweeps. The sampling strategy should match the topology of the parameter space:
* **Continuous / Real Parameters** (e.g., multiplier offsets) $\rightarrow$ Ratio scaling or fractional sweeps.
* **Discrete / Integer Parameters** (e.g., tick counts) $\rightarrow$ Step-wise search.
* **Temporal Parameters** (e.g., millisecond timers) $\rightarrow$ Linear or logarithmic scaling.

This strategy ensures high resolution around the estimated decision boundaries where transitions (e.g. from action to non-action) occur.

---

## 3. Research Report Requirements

Every research report or proposal advancing from **Stage E2 (Counterfactual)** to **Stage E4 (OOS Validation)** must include a dedicated **Threats to Validity** checklist.

### Threats to Validity Checklist
* [ ] **Internal Validity** — Could execution latency, bid-ask spread friction, tracking errors, or data dropouts explain the simulated performance improvements?
* [ ] **External Validity** — Does the proposed relationship generalize across different market regimes, trading sessions (day vs. night), or contract months?
* [ ] **Construct Validity** — Does the indicator measure the intended underlying factor (e.g., does Squeeze actually measure volatility regime, or does it leak trend bias)?
* [ ] **Statistical Validity** — Is the sample size large enough? Are confidence intervals reported? Did the methodology control for multiple comparison bias (avoiding p-hacking)?
* [ ] **Failure Modes** — Under what specific conditions (e.g., contract rollovers, high slippage) does this indicator break down?
* [ ] **Reproducibility** — Are the data inputs, parameters, and analysis scripts archived and executable by other developers or AI agents?

---

## 4. Active Research Registry (Sample)

| Research ID | Title | Highest Stage | Status | Core Hypothesis |
|---|---|---|---|---|
| **R-001** | Bollinger Bands Position at Release | E1 | Active (Telemetry) | Target exit timing correlates with statistical price position on bands. |
| **R-002** | Release Decision Reproducibility | E2 | Accepted | 34/34 RELEASE decisions reproduced deterministically. Decision ≠ Execution proven. |
| **R-003** | Counterfactual Sensitivity — Phase 3A | E3 | ACCEPTED | NON_BINDING_WITHIN_TESTED_RANGE (0/272 flips). Evidence model boundary discovered: Decision ≠ Timing. |
| **R-004** | Half-life estimation | E0 | Idea | Reversion speed within regime informs dynamic timeout. |

<!-- 2026-07-17 Gemini CLI: finalized Replay & Counterfactual Methodology v1.0 README.md with 5-Layer structure -->
