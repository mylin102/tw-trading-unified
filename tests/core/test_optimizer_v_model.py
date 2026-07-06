"""
V-Model Verification: core/parameter_optimizer.py
Verifies parallel grid search execution and result aggregation.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from core.parameter_optimizer import GridSearchOptimizer
from core.backtest_engine import AssetProfile, AssetType

def test_grid_search_parallel_execution():
    # 1. Setup dummy data
    dates = [datetime(2026, 1, 1) + timedelta(minutes=5*i) for i in range(100)]
    df = pd.DataFrame({"Close": np.linspace(100, 110, 100), "ticker": "TMF"}, index=dates)
    
    profile = AssetProfile(asset_type=AssetType.FUTURES, point_value=200, margin_per_lot=100000)
    optimizer = GridSearchOptimizer(profile=profile, initial_capital=1_000_000)
    
    # 2. Define Grid (small for test)
    # Testing counter_vwap as it has a valid StrategyBase subclass
    param_grid = {
        "entry_score": [5, 10],
        "atr_mult": [1.5, 2.0]
    }

    # 3. Run
    results_df = optimizer.run_sweep("counter_vwap", df, param_grid, max_workers=2)
    
    # 4. Assertions
    assert len(results_df) == 4 # 2x2 combinations
    assert "entry_score" in results_df.columns
    assert "atr_mult" in results_df.columns
    assert "total_pnl" in results_df.columns
    
    # Check that all combinations were tested
    combinations = set(zip(results_df["entry_score"], results_df["atr_mult"]))
    assert (5, 1.5) in combinations
    assert (10, 2.0) in combinations
