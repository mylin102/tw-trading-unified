# strategy_reorder_simulator.md

## Purpose

`strategy_reorder_simulator.py` is a lightweight analysis tool for testing
whether changing router candidate priority may improve expected outcomes.

It works on top of attribution CSV files already produced by the attribution system.

---

## What It Does

The simulator compares:

- actual router winner
- simulated winner under a different candidate order

Then it estimates impact using historical average PnL from:

- `trade_attribution_log.csv`

This makes it useful for:

1. comparing different candidate orders
2. spotting strategies that may deserve higher priority
3. evaluating router structure, not just raw strategy PnL

---

## Important Limitation

This version is a **best-effort structural simulator**, not a full shadow replay engine.

That means:

- it knows which strategies were present in the candidate list
- it knows the actual winner
- it does **not** know whether shadowed strategies would truly have fired
  unless shadow replay data exists

So the simulated winner is:

> the first candidate present in the bar under the new priority order

This is useful for router-structure analysis, but it is **not the same as counterfactual truth**.

For higher-confidence analysis, use future shadow replay data.

---

## Required Input Files

Place these files in the input directory:

- `router_evaluation_log.csv`
- `trade_attribution_log.csv`

Optional:
- `strategy_signal_log.csv`

---

## Output Files

The script generates:

- `order_1_detail.csv`
- `order_1_summary.csv`
- `order_2_detail.csv`
- `order_2_summary.csv`
- `simulation_summary.csv`
- `simulation_config.json`

---

## Key Fields

### Per-bar detail

- `actual_winner`
- `simulated_winner`
- `actual_expected_pnl`
- `simulated_expected_pnl`
- `expected_pnl_delta`

### Summary

- `bars`
- `changed_count`
- `change_rate`
- `actual_expected_pnl_sum`
- `simulated_expected_pnl_sum`
- `expected_pnl_delta_sum`

---

## Basic Usage

```bash
python strategy_reorder_simulator.py   --input-dir ./data/attribution   --output-dir ./reports/reorder_sim   --order counter_vwap,spring_upthrust,kbar_feature   --order kbar_feature,counter_vwap,spring_upthrust
```

---

## Filter by Symbol

```bash
python strategy_reorder_simulator.py   --input-dir ./data/attribution   --output-dir ./reports/reorder_tx   --order counter_vwap,spring_upthrust,kbar_feature   --order kbar_feature,counter_vwap,spring_upthrust   --symbol TX
```

---

## Filter by Regime

```bash
python strategy_reorder_simulator.py   --input-dir ./data/attribution   --output-dir ./reports/reorder_weak   --order counter_vwap,spring_upthrust,kbar_feature   --order kbar_feature,counter_vwap,spring_upthrust   --regime WEAK
```

---

## Filter by Symbol + Regime

```bash
python strategy_reorder_simulator.py   --input-dir ./data/attribution   --output-dir ./reports/reorder_tx_weak   --order counter_vwap,spring_upthrust,kbar_feature   --order kbar_feature,counter_vwap,spring_upthrust   --symbol TX   --regime WEAK
```

---

## Minimum Trade Threshold

The simulator estimates expected PnL using average realized PnL from past trades.

To avoid overreacting to tiny samples, you can require a minimum number of trades:

```bash
python strategy_reorder_simulator.py   --input-dir ./data/attribution   --output-dir ./reports/reorder_sim   --order counter_vwap,spring_upthrust,kbar_feature   --order kbar_feature,counter_vwap,spring_upthrust   --min-trades-per-strategy 10
```

If a strategy does not meet the minimum trade threshold, its expected PnL contribution is treated as `0.0`.

---

## Recommended Interpretation

Use this tool to answer questions like:

- If `kbar_feature` were moved earlier, how often would winner selection change?
- Would expected outcome improve under a different candidate order?
- Is current router ordering likely suppressing a profitable strategy?

Do **not** treat this output as final proof that a reorder should go live.
It is a screening tool.

Recommended workflow:

1. run attribution in production / backtest
2. run reorder simulator
3. identify promising candidate orders
4. validate with shadow replay or full backtest
5. only then change live router priority

---

## Next Recommended Upgrade

For stronger conclusions, extend the attribution system with:

- shadow replay logging
- per-bar counterfactual signal truth
- per-order full backtest replay

At that point, the reorder simulator can evolve from:

> structural approximation

to

> true counterfactual router optimization

---
