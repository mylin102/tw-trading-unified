# Research Dataset Contract v1.0

> **Status:** FROZEN (v1.0)
>
> A structured contract defining the schema, layers, and derived metrics for the MTS quantitative research database.

---

## 1. Why This Contract Exists

To ensure all future research (e.g., R-001, R-004, R-007) operates on a standardized, reproducible, and immutable dataset, we codify the **Research Dataset Contract**. 

This contract prevents schema drift, structures raw logs into an analytic-ready format, and establishes the schema boundaries for point-in-time and trajectory analysis.

```text
JSONL Audit Logs ──► Rebuild Pipeline (core/trade_dataset.py) ──► Parquet Analytical Views (Contract v1.0)
```

---

## 2. The Four-Layer Data Architecture

Every completed spread trade is decomposed into four distinct layers of evidence. The pipeline compiles these layers into four corresponding Parquet tables: `trade_facts.parquet`, `trade_snapshots.parquet`, `trade_decisions.parquet`, and `trade_outcomes.parquet`.

```text
                  MTS Research Dataset Layers

           ┌──────────────────────────────────────┐
           │ Layer 1: Outcome & Metadata (Facts)  │
           ├──────────────────────────────────────┤
           │ Layer 2: Timeline & Durations        │
           ├──────────────────────────────────────┤
           │ Layer 3: State Snapshots             │
           ├──────────────────────────────────────┤
           │ Layer 4: Outcome Labels (Excursions) │
           └──────────────────────────────────────┘
```

### Layer 1: Outcome & Metadata (Facts)
*Defines the identity, structural configuration, and raw realized PnL for the trade.*
* **Trade Identity:** `trade_id`, `session` (Day/Night), `direction` (Long/Short spread).
* **Execution Contracts:** `near_contract`, `far_contract` tickers.
* **Direct Outputs:** `near_exit_price`, `far_exit_price`, `release_price`, `pnl_total` (TWD).
* **Data Quality:** `data_quality` flag (ok, stale_quote, tick_gap).

### Layer 2: Timeline & Durations
*Logs the timestamps of critical state transitions and calculates phase durations.*
* **Transition Timestamps:** `entry_time`, `release_time`, `exit_time`.
* **Derived Durations:**
  * **Release Delay (Entry → Release):** Duration the trade was held as a dual-leg spread before a leg was released.
  * **Trail Duration (Release → Exit):** Duration the remaining leg was managed individually.
  * **Total Holding Time (Entry → Exit):** Total trade lifespan.

### Layer 3: State Snapshot
*Captures the exact market variables and indicator values observed by the decision engine immediately preceding any action.*
* **At Entry / Release / Exit:**
  * Market prices: `price_near`, `price_far`, `spread`.
  * Spread indicators: `z_score`, `spread_mean`, `spread_std`.
  * Volatility/Trend indicators: `atr`, `bb_position`, `bb_width`, `sqz_on`, `regime`.

### Layer 4: Outcome Label (Excursions)
*Logs the maximum potential profit/loss space observed after key decisions, used for strategy optimization.*
* **Released Leg Excursions (Post-Release):** `mfe_released_leg`, `mae_released_leg` (Maximum Favorable / Adverse Excursion).
* **Remaining Leg Excursions (Post-Release):** `mfe_remaining_leg`, `mae_remaining_leg`.
* **Combined Excursions:** `mfe_combined`, `mae_combined` (absolute trade boundary).
* **Trail Metrics:** `trail_distance` (points between peak and trail exit).

---

## 3. Derived Research Metrics

To evaluate the efficiency of leg management and mobile exits, the following derived metrics are standardized for E2 (Counterfactual) analysis:

### A. Release Efficiency (Post-Release Profit Preservation Efficiency)

Measures the efficiency of post-release profit preservation, calculating how effectively the exit algorithm retained paper profits on the remaining leg after the first leg was released.

$$Release\ Efficiency = \frac{Final\ Net\ PnL}{Second\ Leg\ Peak\ PnL}$$

* **Noise Gating Invariant:** This metric is strictly defined and evaluated only when $\text{Second Leg Peak PnL} > \epsilon$ (where $\epsilon = 100$ TWD). This prevents divide-by-zero errors and excludes low-absolute-value noise.
* *Case A (High Efficiency):* Peak $PnL = +2,500$ TWD, Final $PnL = +2,400$ TWD $\rightarrow$ **96% Efficiency** (Exit managed optimally).
* *Case B (Low Efficiency / Profit Leakage):* Peak $PnL = +2,500$ TWD, Final $PnL = +400$ TWD $\rightarrow$ **16% Efficiency** (Exit algorithm gave back too much; trail is too loose or slow).
* *Case C (No Trend):* Peak $PnL = +400$ TWD, Final $PnL = +300$ TWD $\rightarrow$ **75% Efficiency** (Market had no momentum; the low absolute PnL is a structural regime issue, not an exit management issue).

### B. Peak Capture Ratio

Measures the percentage of the absolute maximum favorable excursion captured by the exit fill.

$$Peak\ Capture\ Ratio = \frac{Realized\ PnL}{MFE\ (in\ points) \times Point\ Value}$$

---

## 4. Dataset Invariants

To ensure data integrity, any generation of the research database must satisfy the following invariants:

| Invariant | Description | Enforcement Action |
|---|---|---|
| **Uniqueness** | Exactly one row in `trade_facts` and `trade_outcomes` per `trade_id`. | Fail build on duplicate. |
| **Monotonicity** | Timestamps must be strictly chronological: $\text{Entry Time} \le \text{Release Time} \le \text{Exit Time}$. | Discard trade from analytical view. |
| **Non-negativity** | Durations ($\text{Release Delay}$, $\text{Trail Duration}$, $\text{Holding Time}$) must be $\ge 0$. | Discard trade from analytical view. |
| **Immutability of Facts** | Excursions (MFE/MAE) and historical prices are objective facts and must not depend on future strategy versions or rebuilds. | Deterministic reconstruction from log source of truth. |

---

## 5. Research KPIs (Evidence-First Metrics)

During the data accumulation phase, the primary KPIs shift from short-term trading PnL to database and research process quality:

* **Dataset Completeness:** Ratio of successfully compiled analytical rows to raw trading log lines (Target: $\ge 99.5\%$).
* **State Coverage:** Spread of recorded trades across orthogonal regimes and sessions (Target: minimum 5 trades per session/regime combination).
* **Schema Stability:** Count of breaking changes to `v1.x` schemas (Target: 0).
* **Replay Reproducibility:** Percentage of past decisions that can be reproduced 100% deterministically by the Point Replay engine (Target: 100%).
* **Research Cadence:** Generation of a new E2 Evidence Report for every batch of 50–100 completed trades.

---

## 6. Transition to Evidence Accumulation Phase

The project has officially transitioned from the **Strategy Development Phase** to the **Evidence Accumulation Phase**. 

### Guidelines for this Phase:
1. **Schema Freeze:** No modifications may be made to the database schemas defined in `core/trade_dataset.py` during v1.x of the methodology.
2. **Strategy Freeze:** The trading strategy parameters and FSM engine must remain unchanged to ensure the telemetry data is homogeneous and free from parameter contamination.
3. **Periodic Audits:** Analysis scripts should aggregate metrics (Average Trail Duration, Median Second-Leg PnL, Release Efficiency) periodically (e.g., every 50 complete trades) to produce E2 Evidence Reports.

<!-- 2026-07-17 Gemini CLI: updated Research Dataset Contract v1.0 with invariants, KPIs, and efficiency bounds -->
