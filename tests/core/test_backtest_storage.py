"""
Unit tests for core/backtest_storage.py
Verifies experiment saving, registry updates, and Parquet data integrity.
"""
import pytest
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
import shutil
import json

from core.backtest_engine import BacktestResult
from core.backtest_storage import ExperimentTracker

def test_experiment_tracking_lifecycle():
    test_path = "data/test_backtests"
    # Cleanup previous test data
    shutil.rmtree(test_path, ignore_errors=True)
    
    tracker = ExperimentTracker(base_path=test_path)
    
    # 1. Create dummy result
    trades = pd.DataFrame([
        {"timestamp": datetime(2026, 1, 1), "action": "BUY", "price": 100, "pnl": 0},
        {"timestamp": datetime(2026, 1, 2), "action": "EXIT", "price": 110, "pnl": 10}
    ])
    equity = pd.Series([1000, 1010], index=[datetime(2026, 1, 1), datetime(2026, 1, 2)])
    metrics = {"total_pnl": 10, "win_rate": 1.0}
    
    result = BacktestResult("TestStrategy", trades, equity, metrics)
    params = {"fast_ma": 10, "slow_ma": 20}
    
    # 2. Save
    exp_id = tracker.save_experiment(result, params, tag="unit_test")
    
    # Check disk
    exp_dir = Path(test_path) / "experiments" / exp_id
    assert exp_dir.exists()
    assert (exp_dir / "trades.parquet").exists()
    assert (exp_dir / "equity.parquet").exists()
    assert (exp_dir / "meta.json").exists()
    
    # Check registry
    with open(Path(test_path) / "registry.json", "r") as f:
        registry = json.load(f)
        assert len(registry) == 1
        assert registry[0]["exp_id"] == exp_id
        assert registry[0]["tag"] == "unit_test"
    
    # 3. List
    exps = tracker.list_experiments(strategy="TestStrategy")
    assert len(exps) == 1
    
    # 4. Load
    loaded_res, loaded_meta = tracker.load_result(exp_id)
    assert loaded_res.strategy_name == "TestStrategy"
    assert loaded_res.metrics["total_pnl"] == 10
    assert len(loaded_res.trades) == 2
    assert loaded_meta["params"]["fast_ma"] == 10
    
    # 5. Delete
    tracker.delete_experiment(exp_id)
    assert not exp_dir.exists()
    assert len(tracker.list_experiments()) == 0

    # Final Cleanup
    shutil.rmtree(test_path, ignore_errors=True)
