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

---

## Wave 5.5: Session-Aware Adaptive Parameters (Implemented 2026-04-16)

### Findings from vectorbt Session Sweep
Through a vectorized sweep of 100,000 bars, we identified that Day and Night sessions require distinct protection profiles:
- **DAY Session**: High volatility. Optimal BE at **20 pts**, Trail at **120 pts** (Fast protection, wide breathing room).
- **NIGHT Session**: Stronger inertia. Optimal BE at **70 pts**, Trail at **140 pts** (Delayed protection to avoid noise, capturing long trends).

### Implementation Details
- **Monitor Core**: Moved SL/Trail logic to `on_tick` level for sub-bar protection.
- **Engine Core**: Upgraded `BacktestEngine` and `PaperTrader` to support continuous `trail_points` and `break_even_trigger`.
- **Strategy Plugins**: Updated `CounterVWAP` to automatically toggle parameters based on `bar.index.hour`.

---

## Verification & Final Status (2026-04-16)
- **Unit Tests**: `tests/analysis/test_adaptive_analyzer.py` passed.
- **Backtest Comparison**: `counter_vwap` showed **+199k TWD** improvement with adaptive trailing.
- **Process Check**: Night session monitor is active and utilizing new logic.
