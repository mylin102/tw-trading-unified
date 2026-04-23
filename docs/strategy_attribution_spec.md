# strategy_attribution_spec.md

## Goal

This document defines how to measure strategy performance under a
regime-gated, priority-ordered, first-valid-signal router.

The objective is to evaluate:

1. Strategy access opportunity
2. Router-induced starvation / priority shadowing
3. Realized trading performance
4. Counterfactual opportunity (optional, via shadow replay)

---

## 1. Attribution Layers

### 1.1 Exposure / Opportunity Attribution

Measures how often a strategy has the chance to act.

Fields:

- candidate_count
- eval_count
- winner_count
- shadowed_count
- regime_mismatch_count
- no_signal_count

---

### 1.2 Execution Attribution

Measures actual trading performance.

Fields:

- trade_count
- win_count
- loss_count
- avg_pnl
- total_pnl
- avg_mae
- avg_mfe
- avg_hold_bars
- profit_factor
- expectancy

---

### 1.3 Router Priority Attribution

Measures how routing order affects strategy outcomes.

Key concept:
- Higher priority strategies may suppress lower ones

---

## 2. Core Logs

### 2.1 router_evaluation_log

Per-bar, per-strategy evaluation.

Fields:

- timestamp
- symbol
- regime
- candidate_order
- strategy_name
- status (candidate / no_signal / winner / shadowed / mismatch)
- evaluated (True/False)
- winner (True/False)

---

### 2.2 strategy_signal_log

Logs all generated signals.

Fields:

- timestamp
- strategy_name
- regime
- side
- signal_type
- selected (True/False)

---

### 2.3 trade_attribution_log

Per executed trade.

Fields:

- trade_id
- entry_time
- exit_time
- strategy_name
- regime_at_entry
- side
- entry_price
- exit_price
- pnl
- mae
- mfe
- hold_bars

---

## 3. Key Metrics

### Router Metrics

- Candidate Rate = candidate_count / total_bars
- Evaluation Rate = eval_count / candidate_count
- Shadow Rate = shadowed_count / candidate_count
- Win Conversion = winner_count / eval_count

---

### Execution Metrics

- Trade Conversion = trade_count / winner_count
- Win Rate = win_count / trade_count
- Expectancy = avg_win * win_rate - avg_loss * loss_rate
- Profit Factor = gross_profit / gross_loss

---

## 4. Starvation Index

Measures how often a strategy is blocked by higher-priority strategies.

starvation_index = 1 - (eval_count / candidate_count)

Interpretation:

- > 0.7 : severe starvation
- 0.4 ~ 0.7 : moderate
- < 0.4 : acceptable

---

## 5. Shadow Replay (Optional Advanced Mode)

In research mode:

- Continue evaluating strategies after a winner is found
- Record hypothetical signals

Purpose:

- Detect missed opportunities due to routing order
- Compare actual vs potential best strategy

---

## 6. Design Principle

This system evaluates not only:

- strategy performance

but also:

- routing structure efficiency

---

## 7. Summary

Strategy attribution in this system must answer:

1. Did the strategy have access?
2. Was it evaluated?
3. Did it produce signals?
4. Was it selected?
5. Did it generate profit?

