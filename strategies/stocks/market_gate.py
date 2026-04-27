"""Market Gate — regime-based gating for stock entry.

Uses skew signal from futures system to answer "should I be trading right now?"
Replaces unconditional strategy scanning with market-aware decision making.

Gate output:
  ALLOW_LONG  — normal operation, all strategies eligible
  REDUCE_SIZE — cut position size to 50%, only high-conviction entries
  BLOCK_LONG  — no new long entries, only manage existing positions
"""

import os
import json
import time
from typing import Optional
from pathlib import Path


# ── Config ──────────────────────────────────────────────────────────
SKEW_SIGNAL_PATH = Path(__file__).parent.parent / "data" / "skew_signal.json"
SKEW_MAX_AGE_SECS = 300  # 5 min — if skew data is older than this, gate is conservative
DEFAULT_REGIME = "CHOP"  # safest default when no signal available
REGIME_GATE_MAP = {
    "BULL":      "ALLOW_LONG",
    "STRONG":    "ALLOW_LONG",
    "WEAK":      "REDUCE_SIZE",
    "CHOP":      "REDUCE_SIZE",
    "BEAR":      "BLOCK_LONG",
    "CRASH":     "BLOCK_LONG",
}


def _read_skew_signal() -> Optional[dict]:
    """Read latest skew signal from disk."""
    try:
        if not SKEW_SIGNAL_PATH.exists():
            return None
        with open(SKEW_SIGNAL_PATH, "r") as f:
            data = json.load(f)
        return data
    except (json.JSONDecodeError, OSError):
        return None


def _skew_is_fresh(data: dict) -> bool:
    """Check if skew signal is recent enough to trust."""
    ts = data.get("timestamp", 0)
    return (time.time() - ts) < SKEW_MAX_AGE_SECS


def get_market_regime() -> str:
    """Resolve current market regime from skew signal.

    Priority:
      1. Fresh skew signal from futures system
      2. Default (CHOP) if no signal or stale
    """
    data = _read_skew_signal()
    if data and _skew_is_fresh(data):
        regime = data.get("regime", DEFAULT_REGIME)
        return regime
    return DEFAULT_REGIME


def get_gate() -> str:
    """Get the current gate state: ALLOW_LONG | REDUCE_SIZE | BLOCK_LONG."""
    regime = get_market_regime()
    return REGIME_GATE_MAP.get(regime, "REDUCE_SIZE")


def get_size_multiplier() -> float:
    """Position size multiplier based on gate state."""
    gate = get_gate()
    if gate == "ALLOW_LONG":
        return 1.0
    elif gate == "REDUCE_SIZE":
        return 0.5
    else:
        return 0.0


def classify_strategy(strategy_name: str) -> str:
    """Classify a strategy as 'breakout' (trend-following) or 'reversion' (mean-reverting).

    Used by market gating to match strategy type to regime.
    """
    breakout_strategies = {
        "canslim_breakout",
        "momentum_breakout",
        "scout_strategy",
        "it_window_dressing",
    }
    reversion_strategies = {
        "mean_reversion",
        "kd_mean_reversion",
        "bb_bounce",
        "ema_pullback",
        "mean_reversion_enhanced",
        "arbitrage_lite",
    }
    if strategy_name in breakout_strategies:
        return "breakout"
    if strategy_name in reversion_strategies:
        return "reversion"
    return "neutral"


def strategy_allowed(strategy_name: str, regime: Optional[str] = None) -> bool:
    """Check if a specific strategy is allowed in the current regime.

    BREAKOUT strategies: only in TREND/BULL regimes
    REVERSION strategies: only in CHOP/WEAK regimes
    NEUTRAL strategies: always allowed (but subject to gate)
    """
    if regime is None:
        regime = get_market_regime()
    strategy_type = classify_strategy(strategy_name)
    
    trend_regimes = {"BULL", "STRONG"}
    chop_regimes = {"WEAK", "CHOP"}
    
    if strategy_type == "breakout":
        return regime in trend_regimes
    elif strategy_type == "reversion":
        return regime in chop_regimes or regime == "NORMAL"
    else:
        return True
