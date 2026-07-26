# ADR-016: Policy J Causal Counterfactual Evidence Framework & Promotion Contract

* **Status**: ACCEPTED / IN-PROGRESS (Wave J2-A)
* **Date**: 2026-07-26
* **Author**: Gemini CLI & Taiwan Trading Unified Architecture Team
* **Context**: Policy J (Combined UPL Trailing Exit) pure policy, runtime shadow evaluator, JSONL telemetry writer, and Port 8500 read-only visualization have been fully implemented (J1 ~ J1.5-C). Before any live execution (`execution_enabled=true`) can be authorized, a rigorous causal counterfactual evidence framework must be established to evaluate whether Policy J empirically improves trading outcomes without look-ahead bias or in-sample overfitting.

---

## 1. Core Primary Estimands (主要評估指標)

1. **$\Delta\text{NetPnL}$ (淨損益差異)**:
   $$\Delta\text{NetPnL} = \text{Hypothetical Policy J Net Exit PnL} - \text{Actual Strategy Final Net PnL}$$
   - $\Delta\text{NetPnL} > 0$: Policy J successfully lock-in profits / avoided drawdowns.
   - $\Delta\text{NetPnL} < 0$: Policy J exited prematurely, sacrificing upside.

2. **PED (Post-Exit Drawdown / Peak-to-Exit Profit Erosion)**:
   $$\text{PED}_{\text{actual}} = \text{Actual MFE Net PnL} - \text{Actual Final Net PnL}$$
   $$\text{PED}_{\text{policy\_j}} = \text{Actual MFE Net PnL} - \text{Hypothetical Policy J Net Exit PnL}$$
   $$\text{PED}_{\text{improvement}} = \text{PED}_{\text{actual}} - \text{PED}_{\text{policy\_j}}$$

---

## 2. Dataset Taxonomy & Separation (資料集架構分層)

- **Dataset A (Raw Telemetry)**: Append-only JSONL files (`exports/telemetry/policy_j/policy_j_shadow_YYYYMMDD.jsonl`).
- **Dataset B (Trade-Level Counterfactual Facts)**: Single record per trade lifecycle containing `CounterfactualTradeFact` with actual outcomes, hypothetical exits under `FillModel.EXECUTABLE` / `CONSERVATIVE`, and eligibility taxonomy.
- **Dataset C (Parameter Sweep Results)**: Out-of-sample parameter sweep results across activation / giveback pairs (`config_hash`).

---

## 3. Fill Pricing Model & Latency Assumptions (成交定價與滑價模型)

- **FillModel.EXECUTABLE**: Bid/Ask executable quote + 1 tick slippage buffer + fees/tax.
- **FillModel.CONSERVATIVE**: Bid/Ask executable quote + 2 ticks slippage buffer + fees/tax.
- **FillModel.IDEAL**: Mid-quote point-in-time valuation (Reference baseline only; NOT used for promotion gates).

---

## 4. Exclusion Taxonomy (無偏見排除分類)

Samples are strictly classified with `eligible_for_analysis` boolean and `exclusion_reason`:
- `NONE`: Fully eligible dual-leg SPREAD trade.
- `SINGLE_LEG_ONLY`: Incomplete single-leg position.
- `QUOTE_STALE`: Near or far quote age > 1000ms.
- `RESTART_INCOMPLETE`: PM2 restart occurred during trade lifecycle with missing facts.
- `EMERGENCY_FLATTEN`: Manual or emergency flatten override.
- `TELEMETRY_GAP`: Missing JSONL telemetry line.
- `PNL_RECON_MISMATCH`: Broker PnL reconciliation discrepancy > 1 pt.

---

## 5. Promotion Gates for Execution Authorization (升級至 J3 下單前置門禁)

Execution authorization (`execution_enabled=true`) MUST satisfy all 10 gates:
1. Holdout total $\Delta\text{NetPnL} > 0$.
2. Holdout median $\Delta\text{NetPnL} \ge 0$.
3. Downside tail loss (P10 / P05) has NOT deteriorated.
4. Positive $\Delta\text{NetPnL}$ across at least 2 distinct sessions (Day & Night).
5. Leave-one-trade-out sensitivity: No single extreme trade accounts for > 40% of total $\Delta\text{NetPnL}$.
6. Parameter neighborhood stability (neighboring activation/giveback parameters maintain positive $\Delta\text{NetPnL}$).
7. Telemetry coverage $\ge 95\%$ of eligible trades.
8. Conservative fill model maintains positive net PnL.
9. All calculations net of friction (commission + tax + bid-ask spread).
10. Zero regression in MTS lifecycle differential decision parity.
