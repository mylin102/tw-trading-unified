# ATR Optimization Technical Analysis

**Analysis Date:** 2026-05-13
**Focus Area:** TMF Spread Strategy ATR-based Dynamics

## Executive Summary
This document analyzes the current implementation of ATR-based volatility filtering and scaling within the `tmf_spread` strategy and its corresponding backtest environment. The system has transitioned from fixed point-based risk management to a dynamic model that adapts to market volatility, using the 1-minute ATR as the primary scaling factor.

## 1. ATR-Based Entry Filtering (`min_atr`)

### Implementation
- **File:** `strategies/plugins/futures/active/tmf_spread.py`
- **Parameter:** `min_atr` (Default: `0.0`)
- **Logic:** The strategy performs a "Staleness and Volatility Gate" before entry evaluation. If the current bar's ATR is below `min_atr`, the entry is skipped with the reason `ATR_TOO_LOW`.

### Rationale
Low volatility environments often result in "fakeouts" where the spread hits a z-score extreme but lacks the momentum to trigger a meaningful directional release. Filtering by `min_atr` ensures the strategy only operates when there is sufficient "fuel" for a trend continuation.

### Observations
- In `backtest_spread_v2.py`, `spread_std` is used as a proxy for `atr` if the latter is missing in the data.
- The backtest currently evaluates scenarios for `min_atr` values of `0.0, 10.0, 12.0, 15.0`.

## 2. ATR-Scaled Release Stops and Trailing Exits

### Threshold Calculation (`_get_thresholds`)
The strategy dynamically calculates two critical thresholds every bar:

1.  **Release Stop (`atr_multiplier_stop`):**
    - **Purpose:** The losing leg's stop loss that triggers the "release" of the winning leg.
    - **Formula:** `max(5.0, atr * atr_multiplier_stop)`
    - **Default Multiplier:** `1.5`

2.  **Trailing Distance (`atr_multiplier_trail`):**
    - **Purpose:** The trailing stop distance for the remaining single leg after release.
    - **Formula:** `max(10.0, atr * atr_multiplier_trail)`
    - **Default Multiplier:** `2.0`

### Position Management Logic
- **Release Phase:** If either the Near or Far leg PnL (in points) drops below `-release_stop`, that leg is closed, and the strategy enters "Trailing mode" for the other leg.
- **Trailing Phase:** The strategy tracks the `peak` (for Long) or `nadir` (for Short) of the remaining leg and exits when the price retraces by `trail_dist`.

## 3. Strategy-Backtest Relationship

### Indicator Handling
The `backtest_spread_v2.py` script bridges the gap between raw CSV data and the strategy's requirements:
- **Proxying:** It maps `spread_std` (standard deviation of the spread) to the `atr` field if `atr` is not present in the CSV. This is a critical assumption that standardizes volatility measurement across different data sources.
- **Config Injection:** The backtester injects `atr_multiplier_stop` and `atr_multiplier_trail` via the `StrategyContext` config, allowing for rapid parameter sweeping without modifying code.

### Friction and Cost Modeling
- The backtester implements a realistic cost model: `FEE_PER_SIDE (10.0 TWD)` + `TAX_RATE (0.00002)`.
- It tracks "Friction" as `(Fees + Taxes) / abs(Gross PnL)`.
- **Finding:** ATR scaling helps reduce friction by ensuring that stop/trail distances are wide enough to cover transaction costs (typically ~8 pts for a TMF round-trip) while remaining tight enough to preserve capital.

## 4. Technical Risks & Recommendations

### Risks
- **Indicator Staleness:** The strategy evaluates `spread_age_minutes`. If the ATR/Spread data is stale, the entry is blocked, but once in a position, the strategy relies on the last known ATR for management.
- **Floor Limits:** Current floors (`5.0` for stop, `10.0` for trail) might be too tight if the TMF tick value is high or slippage is significant.

### Recommendations
1.  **Direct ATR Calculation:** Ensure the data pipeline (`core/data_manager.py` or similar) provides a true ATR (High-Low range) rather than relying on `spread_std` proxying in production.
2.  **Dynamic Floor:** Consider scaling the floor values based on the current instrument price to maintain a constant basis-point risk.
3.  **State Persistence:** The `_write_mts_state` function correctly logs the dynamic thresholds. This should be utilized in the Streamlit dashboard to visualize the "Current Stop" and "Current Trail" levels in real-time.

---
*Created by GSD Mapper*
