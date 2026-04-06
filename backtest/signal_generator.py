import numpy as np
import pandas as pd
from typing import Dict, Tuple, Any, Optional
from strategies.futures.entry_strategies import STRATEGIES as FUTURES_STRATEGIES
from strategies.stocks.entry_strategies import STOCK_STRATEGIES
from core.strategy_schema import StrategyParams

# Merge registries for universal access
ALL_STRATEGIES = {**FUTURES_STRATEGIES, **STOCK_STRATEGIES}


def apply_strategy_filters(
    df: pd.DataFrame,
    params: StrategyParams,
) -> pd.DataFrame:
    """
    Apply squeeze pattern StrategyParams filters to a DataFrame.
    Instead of removing rows (which breaks time-series continuity for Numba engine),
    this zeros out signal columns (fired, sqz_on, mom_state) on non-matching rows,
    preserving OHLCV continuity for the backtest engine.
    """
    result = df.copy()

    if params.patterns:
        if "pattern" not in result.columns:
            from strategies.stocks.squeeze_patterns import apply_squeeze_patterns
            result = apply_squeeze_patterns(result)
        mask = ~result["pattern"].isin(params.patterns)
        _suppress_signals(result, mask)

    if params.min_momentum is not None and "mom_state" in result.columns:
        mask = result["mom_state"] < params.min_momentum
        _suppress_signals(result, mask)

    if params.max_momentum is not None and "mom_state" in result.columns:
        mask = result["mom_state"] > params.max_momentum
        _suppress_signals(result, mask)

    if params.require_squeeze_on and "sqz_on" in result.columns:
        mask = ~result["sqz_on"]
        _suppress_signals(result, mask)

    if params.require_fired and "fired" in result.columns:
        mask = ~result["fired"]
        _suppress_signals(result, mask)

    if params.min_value_score is not None and "value_score" in result.columns:
        mask = result["value_score"] < params.min_value_score
        _suppress_signals(result, mask)

    if params.allowed_regimes and "market_regime" in result.columns:
        mask = ~result["market_regime"].isin(params.allowed_regimes)
        _suppress_signals(result, mask)

    return result


def _suppress_signals(df: pd.DataFrame, mask: pd.Series) -> None:
    """Zero out signal columns on masked rows to prevent entry signals."""
    if "fired" in df.columns:
        df.loc[mask, "fired"] = False
    if "sqz_on" in df.columns:
        df.loc[mask, "sqz_on"] = False
    if "mom_state" in df.columns:
        df.loc[mask, "mom_state"] = 0

def build_state_optimized(
    df_5m_np: Dict[str, np.ndarray], 
    df_15m_np: Dict[str, np.ndarray], 
    idx: int, 
    df_5m_full: pd.DataFrame, 
    cfg: Dict
) -> Dict[str, Any]:
    """
    Fast state builder using pre-extracted NumPy arrays.
    Replaces the expensive pd.DataFrame slicing inside loops.
    """
    # Build Series-like objects for the strategy functions using current index
    last_5m = pd.Series({k: v[idx] for k, v in df_5m_np.items()})
    
    # For 15m, find the closest previous 15m index (simplified alignment)
    # In a real scenario, we'd pre-calculate the mapping
    last_15m = pd.Series({k: v[idx] for k, v in df_15m_np.items()}) 

    # Handle missing price_round for arbitrage strategy
    price_round = last_5m.get("price_round", last_5m["Close"])

    return {
        "last_5m": last_5m,
        "last_15m": last_15m,
        "df_5m": df_5m_full.iloc[max(0, idx-100):idx+1], # Sliding window for indicators needing history
        "df_5m_full": df_5m_full,
        "idx": idx,
        "score": last_5m.get("score", 0),
        "price_round": price_round,
        "stop_loss_pts": last_5m.get("atr", 30),
        "hour": df_5m_full.index[idx].hour,
        "trend": {
            "trend_long": last_5m.get("bullish_align", False),
            "trend_short": last_5m.get("bearish_align", False)
        }
    }

def generate_signals(
    df_5m: pd.DataFrame, 
    strategy_name: str, 
    cfg: Dict,
    warmup: int = 60
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Converts strategy dict outputs to boolean arrays for vectorized engine.
    Uses NumPy optimization to minimize Pandas overhead.
    """
    if strategy_name not in ALL_STRATEGIES:
        raise ValueError(f"Strategy {strategy_name} not found in any registry.")
        
    strat_entry = ALL_STRATEGIES[strategy_name]
    strategy_fn = strat_entry["func"] if isinstance(strat_entry, dict) else strat_entry
    n = len(df_5m)
    long_signals = np.zeros(n, dtype=bool)
    short_signals = np.zeros(n, dtype=bool)

    if n <= warmup:
        return long_signals, short_signals

    # Pre-extract columns to dict of numpy arrays for speed
    df_5m_np = {col: df_5m[col].values for col in df_5m.columns}
    # For now, assuming 15m is same as 5m or pre-calculated in df_5m
    df_15m_np = df_5m_np 

    for i in range(warmup, n):
        state = build_state_optimized(df_5m_np, df_15m_np, i, df_5m, cfg)
        result = strategy_fn(state, cfg)
        
        if result:
            if result["action"] == "BUY":
                long_signals[i] = True
            elif result["action"] == "SELL":
                short_signals[i] = True

    return long_signals, short_signals
