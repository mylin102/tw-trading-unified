"""
Parameter Optimizer — Grid search and parallel backtesting engine.
Systematically scans parameter ranges to find optimal strategy configurations.
"""
from __future__ import annotations

import itertools
import concurrent.futures
import logging
from typing import Any, Dict, List, Optional

import pandas as pd

from core.strategy_registry import StrategyRegistry
from core.backtest_engine import BacktestEngine, AssetProfile, AssetType


def _run_single_iteration(
    params: Dict[str, Any],
    strategy_name: str,
    df: pd.DataFrame,
    profile_data: Dict[str, Any],
    initial_capital: float
) -> Dict[str, Any]:
    """
    Worker function for parallel grid search.
    Must instantiate its own Registry/Strategy/Engine to avoid pickling issues.
    """
    try:
        # 1. Setup Environment
        reg = StrategyRegistry()
        reg.discover()
        strategy = reg.get(strategy_name)
        if not strategy:
            return {**params, "error": "Strategy not found"}

        # 2. Reconstruct Profile
        profile = AssetProfile(**profile_data)
        engine = BacktestEngine(profile=profile, initial_capital=initial_capital)

        # 3. Run Backtest
        # We pass the full params dict inside a 'params' key to match StrategyBase.init logic
        config = {"params": params}
        result = engine.run(df, strategy, config=config)
        
        # 4. Extract Metrics
        metrics = result.metrics
        # Flatten metrics for the result row
        return {**params, **metrics}
    except Exception as e:
        return {**params, "error": str(e)}


class GridSearchOptimizer:
    """Orchestrates parallel execution of multiple backtest runs."""

    def __init__(self, profile: AssetProfile, initial_capital: float = 1_000_000, logger: Optional[logging.Logger] = None):
        self.profile = profile
        self.initial_capital = initial_capital
        self.logger = logger or logging.getLogger(__name__)

    def run_sweep(
        self,
        strategy_name: str,
        df: pd.DataFrame,
        param_grid: Dict[str, List[Any]],
        max_workers: Optional[int] = None
    ) -> pd.DataFrame:
        """
        Execute a grid search sweep across multiple CPU cores.
        
        Args:
            strategy_name: Name of the strategy plugin to optimize.
            df: Historical DataFrame (OHLCV).
            param_grid: Dict of parameter names to lists of values to test.
            max_workers: Number of parallel processes. Defaults to CPU count.
            
        Returns:
            pd.DataFrame: Results of all combinations with their respective metrics.
        """
        # 1. Generate all combinations
        keys = list(param_grid.keys())
        values = list(param_grid.values())
        combinations = [dict(zip(keys, v)) for v in itertools.product(*values)]
        
        self.logger.info(f"🚀 Starting Grid Search for {strategy_name}")
        self.logger.info(f"📊 Total combinations: {len(combinations)}")

        profile_data = self.profile.model_dump()
        results = []

        # 2. Parallel Execution
        with concurrent.futures.ProcessPoolExecutor(max_workers=max_workers) as executor:
            # Map combinations to futures
            futures = [
                executor.submit(
                    _run_single_iteration, 
                    params, 
                    strategy_name, 
                    df, 
                    profile_data, 
                    self.initial_capital
                )
                for params in combinations
            ]
            
            for i, future in enumerate(concurrent.futures.as_completed(futures)):
                res = future.result()
                results.append(res)
                if (i + 1) % 10 == 0 or (i + 1) == len(combinations):
                    self.logger.info(f"✅ Progress: {i+1}/{len(combinations)} iterations complete.")

        return pd.DataFrame(results)
