# Adaptive Trade Review & Optimization System (GSD Spec)

## Goal
To transform qualitative trade observations into quantitative feedback loops, enabling **Adaptive Trading** where strategy parameters (e.g., stops, thresholds, regime filters) are adjusted automatically based on realized performance and market context.

## Core Methodology

### 1. GSD (Get Shit Done) Approach
- **Contextual Logging**: Capture not just the trade, but the "Why" (Signal Audit) and the "Where" (Market Regime).
- **Quantified Post-mortems**: Automatically join trades with indicator snapshots to calculate alpha decay and edge.
- **Adaptive Waves**: Deploy parameter changes in verified "waves" based on statistical significance.

### 2. gstack (Engineering Ops) approach
- **Data Integrity**: Verify that `trades.csv` and `signals_audit.csv` align perfectly in time.
- **PnL Accuracy**: Strictly enforce fee/tax calculations as per `RULES.md`.
- **Health Monitoring**: Monitor the "Realized vs. Expected" PnL gap to trigger safety cooldowns.

---

## Architecture of the Review Loop

### Phase 1: Data Enrichment (Input)
The system will correlate data from three sources:
1.  **Execution Data**: `exports/trades/*.csv` (Price, Type, Reason).
2.  **Audit Trail**: `logs/market_data/*_signals_audit.csv` (Signal Score, Blocked Reasons).
3.  **Feature Snapshot**: `logs/market_data/*_indicators.csv` (Squeeze Score, Volume Delta, Trend).

### Phase 2: The Adaptive Analyzer (`scripts/analysis/adaptive_analyzer.py`)
A new tool to generate a "Reason Alpha Report":
- **Metrics per Reason**: Win Rate, Profit Factor, and MAE (Max Adverse Excursion) for each `reason` tag (e.g., `CUM_DELTA`, `VWAP_BOUNCE`).
- **Regime Sensitivity**: Effectiveness of strategies across `TRENDING`, `NEUTRAL`, and `SQUEEZE` states.
- **Slippage Audit**: Difference between theoretical signal price and realized fill price.

### Phase 3: Adaptive Recommendations (Output)
The system will generate a `logs/analysis/adaptive_recommendations_{DATE}.json` containing:
- **Tightening**: Suggest increasing thresholds for low-conviction/high-drawdown signals.
- **Loosening**: Suggest increasing size or reducing filters for high-alpha/low-volatility regimes.
- **Switching**: Recommend strategy swaps (e.g., Trend Following -> Mean Reversion) based on recent regime performance.

---

## Action Plan (Wave 5: Adaptive Intelligence)

### Wave 5.1: Structured Analysis Tooling
- [ ] Create `scripts/analysis/adaptive_analyzer.py` to join execution and context data.
- [ ] Generate the first **Adaptive Review Report** for today (2026-04-16).
- [ ] Verify PnL math matches `PaperTrader` logic.

### Wave 5.2: Adaptive Logic Implementation
- [ ] Define "Adaptive Thresholds" in `config/risk_global.yaml`.
- [ ] Implement `core/parameter_optimizer.py` to suggest YAML changes.
- [ ] Add a `gstack` gate: Changes must be reviewed by the `diagnostic_engine.py` before applying.

---

## Verification Protocol
1.  **Integrity Check**: Run `python3 scripts/ops/verify_data_integrity.py`.
2.  **Backtest Alignment**: Feed today's actual regime data into the backtester and compare results.
3.  **Safety Guard**: If daily drawdown exceeds 2x ATR, trigger a "Circuit Breaker" log and freeze parameter updates.

---

## Initial Observations (2026-04-16)
- **Trade**: Short MTX @ 37226 (Reason: `CUM_DELTA`).
- **Context**: Entered during high frequency of blocked signals (cooldown logic).
- **Hypothesis**: The `cooldown_active` guard is preventing over-trading in choppy regimes. We should quantify the "saved loss" from blocked signals.
