# MTS Exit Strategy Optimization Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Optimize the exit logic of the MTS calendar spread strategy (`tmf_spread`) by introducing VWAP-based trailing stops for the remaining leg and volatility-adjusted release stops to maximize realized PnL and reduce drawdowns.

**Architecture:** 
1. **VWAP Trailing Stop:** For the remaining leg, track price relative to its session VWAP (`near_vwap` / `far_vwap`). If the price crosses VWAP in the adverse direction, tighten the trailing stop distance to a configurable fraction (e.g., `0.3x` of the original trail distance) or exit immediately.
2. **Regime-Adaptive Stop:** Dynamically adjust the release stop ATR multiplier based on the Bollinger Band bandwidth (volatility regime indicator).
3. Expose these parameters via `config/futures.yaml` and `config/futures_night.yaml`.

**Tech Stack:** Python, Pandas, pytest

---

### Task 1: Expose VWAP in Backtesting environment

**Files:**
- Modify: `scratch/sweep_tmf_spread_bb_dynamic.py` (or construct a new backtester runner) to include `near_vwap` and `far_vwap` in the simulated K-lines.
- Test: `tests/strategies/test_tmf_spread_atr.py`

**Step 1: Write the failing test**
Add a test in `tests/strategies/test_tmf_spread_atr.py` verifying that the strategy correctly loads `near_vwap` and `far_vwap` from `bar` when present.
```python
def test_vwap_parameters_loaded():
    # Setup context with near_vwap and far_vwap
    # Assert they are readable in strategy context
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/strategies/test_tmf_spread_atr.py -k test_vwap_parameters_loaded -v`
Expected: FAIL (or verify the keys are missing/not asserted)

**Step 3: Write minimal implementation**
Expose the variables in the backtest data loader mock objects.

**Step 4: Run test to verify it passes**
Run: `pytest tests/strategies/test_tmf_spread_atr.py -k test_vwap_parameters_loaded -v`
Expected: PASS

**Step 5: Commit**
```bash
git add tests/strategies/test_tmf_spread_atr.py
git commit -m "test: add vwap parameter loading contract test"
```

---

### Task 2: Implement VWAP-based Exit Acceleration in tmf_spread.py

**Files:**
- Modify: `strategies/plugins/futures/active/tmf_spread.py` (near line 2140 / `_manage_position`)
- Modify: `config/futures.yaml` (add config parameters)

**Step 1: Write the failing test**
In `tests/strategies/test_tmf_spread_atr.py`, add a test where a remaining leg (e.g., FAR leg LONG) experiences a price drop below `far_vwap`, triggering an accelerated exit.
```python
def test_vwap_exit_acceleration_triggered():
    # Setup remaining leg state as LONG
    # Set price below far_vwap
    # Verify exit is triggered or trail tightened
```

**Step 2: Run test to verify it fails**
Run: `pytest tests/strategies/test_tmf_spread_atr.py -k test_vwap_exit_acceleration_triggered -v`
Expected: FAIL

**Step 3: Write minimal implementation**
In `tmf_spread.py`:
- Read `vwap_exit_enabled` (default `false`) and `vwap_tighten_ratio` (default `0.3`) from parameters.
- Inside the remaining leg trailing logic:
  - If `vwap_exit_enabled` is true:
    - Get the corresponding leg's VWAP (`near_vwap` for NEAR, `far_vwap` for FAR).
    - If `remaining_leg == Leg.NEAR` (LONG) and `price < near_vwap`, or `remaining_leg == Leg.NEAR` (SHORT) and `price > near_vwap`:
      - Tighten the trailing stop distance by multiplying it by `vwap_tighten_ratio`, or trigger an immediate exit if `vwap_tighten_ratio == 0.0`.
    - Apply same logic symmetrically for the FAR leg.

**Step 4: Run test to verify it passes**
Run: `pytest tests/strategies/test_tmf_spread_atr.py -k test_vwap_exit_acceleration_triggered -v`
Expected: PASS

**Step 5: Commit**
```bash
git add strategies/plugins/futures/active/tmf_spread.py config/futures.yaml
git commit -m "feat: add vwap-based trailing stop acceleration for remaining leg"
```

---

### Task 3: Perform Grid Sweep to Optimize Exit Parameters

**Files:**
- Create: `scratch/sweep_tmf_spread_exits.py`

**Step 1: Write sweep script**
- Load historical calendar spread data.
- Sweep parameters:
  - `atr_multiplier_stop`: `[1.0, 1.5, 2.0, 2.5]`
  - `atr_multiplier_trail`: `[1.5, 2.0, 2.5, 3.0]`
  - `vwap_exit_enabled`: `[true, false]`
  - `vwap_tighten_ratio`: `[0.0, 0.3, 0.5]`
- Output the Net PnL, Win Rate, and Drawdown for each configuration.

**Step 2: Run the sweep**
Run: `taskpolicy -c background python3 scratch/sweep_tmf_spread_exits.py`

**Step 3: Document findings**
- Create a markdown report `docs/decisions/2026-07-08-mts-exit-optimization-results.md` summarising the optimal parameters.
- Calibrate the production parameters in `config/futures.yaml` and `config/futures_night.yaml`.

**Step 4: Commit**
```bash
git add scratch/sweep_tmf_spread_exits.py config/futures.yaml config/futures_night.yaml docs/decisions/2026-07-08-mts-exit-optimization-results.md
git commit -m "feat: optimize and calibrate MTS exit parameters based on sweep results"
```
