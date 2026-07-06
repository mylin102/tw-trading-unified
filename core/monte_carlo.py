"""
Monte Carlo Simulator — High-performance bootstrap sampling for trading results.
Used to estimate risk distribution and resilience of strategies.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import numpy as np
import pandas as pd


def run_monte_carlo(
    trades: pd.DataFrame,
    initial_capital: float,
    n_simulations: int = 1000,
    ruin_threshold: float = 0.5
) -> Optional[Dict[str, Any]]:
    """
    Perform Monte Carlo simulation by resampling historical trade PnLs.
    
    Returns:
        dict: Containing simulated paths, max drawdowns, and risk metrics.
    """
    if trades.empty:
        return None
    
    # Extract EXIT trades (where PnL is realized)
    exit_trades = trades[trades["action"] == "EXIT"]
    if exit_trades.empty:
        # If no exit trades in DataFrame, assume all rows are PnLs (legacy/other format)
        pnls = trades.get("pnl", trades.get("PnL", pd.Series())).values
    else:
        pnls = exit_trades["pnl"].values
        
    if len(pnls) < 2:
        return None

    n_trades = len(pnls)
    
    # 1. Bootstrap Resampling (Sampling with replacement)
    # Generate random indices: (n_simulations, n_trades)
    indices = np.random.randint(0, n_trades, size=(n_simulations, n_trades))
    sampled_pnls = pnls[indices]
    
    # 2. Construct Equity Paths
    # Start all paths at initial_capital
    paths = np.zeros((n_simulations, n_trades + 1))
    paths[:, 0] = initial_capital
    paths[:, 1:] = initial_capital + np.cumsum(sampled_pnls, axis=1)
    
    # 3. Calculate Maximum Drawdown per Path
    # Using vectorized cumulative maximum to find peaks
    running_max = np.maximum.accumulate(paths, axis=1)
    # Avoid zero division
    drawdowns = (paths - running_max) / np.where(running_max == 0, 1, running_max)
    max_drawdowns = np.min(drawdowns, axis=1)  # Most negative values
    
    # 4. Calculate Risk Metrics
    # Probability of Ruin: proportion of paths hitting threshold
    ruin_level = initial_capital * ruin_threshold
    ruin_mask = np.any(paths <= ruin_level, axis=1)
    prob_of_ruin = np.mean(ruin_mask)
    
    # Confidence Intervals
    mdd_95 = np.percentile(max_drawdowns, 5) # 5th percentile (most severe DDs)
    mdd_median = np.median(max_drawdowns)
    
    return {
        "n_simulations": n_simulations,
        "n_trades": n_trades,
        "paths": paths,
        "max_drawdowns": max_drawdowns,
        "prob_of_ruin": prob_of_ruin,
        "mdd_95": mdd_95,
        "mdd_median": mdd_median
    }
