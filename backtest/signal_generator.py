import numpy as np
import pandas as pd
from typing import Dict, Tuple, Any
from strategies.futures.entry_strategies import STRATEGIES

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

    return {
        "last_5m": last_5m,
        "last_15m": last_15m,
        "df_5m": df_5m_full.iloc[max(0, idx-100):idx+1], # Sliding window for indicators needing history
        "score": last_5m.get("score", 0),
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
    if strategy_name not in STRATEGIES:
        raise ValueError(f"Strategy {strategy_name} not found in registry.")
        
    strat_entry = STRATEGIES[strategy_name]
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
