"""
Unit tests for core/monte_carlo.py
Verifies statistical accuracy of path generation and risk metrics.
"""
import pytest
import pandas as pd
import numpy as np
from core.monte_carlo import run_monte_carlo

def test_monte_carlo_math():
    # 1. Setup predictable trades: 5 wins of 100, 5 losses of -100
    # Total PnL = 0, but sequence matters for MDD
    trades = pd.DataFrame([
        {"action": "EXIT", "pnl": 100},
        {"action": "EXIT", "pnl": 100},
        {"action": "EXIT", "pnl": 100},
        {"action": "EXIT", "pnl": 100},
        {"action": "EXIT", "pnl": 100},
        {"action": "EXIT", "pnl": -100},
        {"action": "EXIT", "pnl": -100},
        {"action": "EXIT", "pnl": -100},
        {"action": "EXIT", "pnl": -100},
        {"action": "EXIT", "pnl": -100},
    ])
    
    initial_capital = 1000
    res = run_monte_carlo(trades, initial_capital, n_simulations=100)
    
    assert res is not None
    assert res["n_simulations"] == 100
    assert res["n_trades"] == 10
    
    # Paths check: all paths should start at 1000
    assert np.all(res["paths"][:, 0] == initial_capital)
    
    # Probability of Ruin check (Threshold 0.5 = 500)
    # With 1000 capital and max loss of 500 (if all losses come first), 
    # since we sample WITH REPLACEMENT, it's possible to get 6+ losses in a row.
    assert 0 <= res["prob_of_ruin"] <= 1.0
    
    # MDD check: max_drawdowns should be non-positive
    assert np.all(res["max_drawdowns"] <= 0)
    assert res["mdd_95"] <= res["mdd_median"]

def test_monte_carlo_empty_data():
    trades = pd.DataFrame()
    res = run_monte_carlo(trades, 1000)
    assert res is None
    
    trades_one = pd.DataFrame([{"action": "EXIT", "pnl": 100}])
    res_one = run_monte_carlo(trades_one, 1000)
    assert res_one is None
