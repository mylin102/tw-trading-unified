import numpy as np
import pandas as pd
from typing import Dict, Tuple, Any, Optional
from strategies.futures.entry_strategies import STRATEGIES as ALL_STRATEGIES_ORIG
from strategies.futures.elite_strategies import ELITE_STRATEGIES
from strategies.stocks.entry_strategies import STOCK_STRATEGIES
from core.strategy_schema import StrategyParams

# Merge registries: elite first, then remaining old ones
ELITE_KEYS = set(ELITE_STRATEGIES.keys())
ALL_STRATEGIES = {}
ALL_STRATEGIES.update(ELITE_STRATEGIES)
for k, v in ALL_STRATEGIES_ORIG.items():
    if k not in ELITE_KEYS:
        ALL_STRATEGIES[k] = v
# Also add stock strategies
ALL_STRATEGIES.update(STOCK_STRATEGIES)


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
    cfg: Dict,
    fire_state: Optional[Dict] = None,
) -> Dict[str, Any]:
    """
    Fast state builder using pre-extracted NumPy arrays.
    Replaces the expensive pd.DataFrame slicing inside loops.

    Args:
        fire_state: Dict tracking squeeze fire state for Counter-VWAP.
            Modified in-place to track fires across bars.
            Keys: pending_dir, bar_idx, high, low
    """
    # Build Series-like objects for the strategy functions using current index
    last_5m = pd.Series({k: v[idx] for k, v in df_5m_np.items()})

    # For 15m, find the closest previous 15m index (simplified alignment)
    last_15m = pd.Series({k: v[idx] for k, v in df_15m_np.items()})

    # Handle missing price_round for arbitrage strategy
    price_round = last_5m.get("price_round", last_5m["Close"])

    state = {
        "last_5m": last_5m,
        "last_15m": last_15m,
        "df_5m": df_5m_full.iloc[max(0, idx-100):idx+1], # Sliding window for indicators needing history
        "df_5m_full": df_5m_full,
        "idx": idx,
        "score": last_5m.get("score", 0),
        "price_round": price_round,
        "stop_loss_pts": last_5m.get("atr", 30),
        "hour": df_5m_full.index[idx].hour if hasattr(df_5m_full.index[idx], 'hour') else 10,
        "trend": {
            "trend_long": last_5m.get("bullish_align", False),
            "trend_short": last_5m.get("bearish_align", False)
        }
    }

    # Inject Counter-VWAP fire state if tracking is enabled
    if fire_state is not None:
        state["fire_pending_dir"] = fire_state["pending_dir"]
        state["fire_bar_idx"] = fire_state["bar_idx"]
        state["fire_high"] = fire_state["high"]
        state["fire_low"] = fire_state["low"]
        state["bar_counter"] = idx

    return state

def generate_signals(
    df_5m: pd.DataFrame,
    strategy_name: str,
    cfg: Dict,
    warmup: int = 60
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Converts strategy dict outputs to boolean arrays for vectorized engine.
    Uses NumPy optimization to minimize Pandas overhead.

    Supports Counter-VWAP with multi-bar squeeze fire tracking.
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
    df_15m_np = df_5m_np

    # Counter-VWAP: initialize fire state tracker
    fire_state = None
    is_counter = strategy_name == "counter_vwap"
    counter_cfg = cfg.get("strategy", {}).get("counter_mode", {})
    confirm_bars = counter_cfg.get("confirm_bars", 5)

    if is_counter:
        fire_state = {
            "pending_dir": 0,
            "bar_idx": 0,
            "high": 0.0,
            "low": 0.0,
        }

    for i in range(warmup, n):
        state = build_state_optimized(df_5m_np, df_15m_np, i, df_5m, cfg, fire_state)
        result = strategy_fn(state, cfg)

        # Counter-VWAP: update fire state after strategy call
        if fire_state is not None:
            fired = df_5m_np.get("fired", np.zeros(n))[i]
            momentum = df_5m_np.get("momentum", np.zeros(n))[i]
            close = df_5m_np["Close"][i]

            # New fire event
            if fired and fire_state["pending_dir"] == 0:
                fire_state["pending_dir"] = 1 if momentum > 0 else -1
                fire_state["bar_idx"] = i
                fire_state["high"] = close
                fire_state["low"] = close

            # Update tracking
            if fire_state["pending_dir"] != 0:
                fire_state["high"] = max(fire_state["high"], close)
                fire_state["low"] = min(fire_state["low"], close)
                bars_since = i - fire_state["bar_idx"]

                # Expire if too many bars without failure confirmation
                if bars_since > confirm_bars:
                    fire_state["pending_dir"] = 0

            # Reset fire state after a counter signal
            if result and result.get("reason") == "COUNTER_VWAP":
                fire_state["pending_dir"] = 0

        if result:
            if result["action"] == "BUY":
                long_signals[i] = True
            elif result["action"] == "SELL":
                short_signals[i] = True

    return long_signals, short_signals
