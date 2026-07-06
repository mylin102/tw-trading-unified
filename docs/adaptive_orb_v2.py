```python
"""
adaptive_orb_v2.py

Breakout Engine v2 + Strategy Router Integration
- ATR normalized breakout
- Scout / Scale entry
- Regime-aware threshold
- Safety guards (ATR floor, session filter)

Author: System Upgrade
"""

from dataclasses import dataclass


# =========================
# Config
# =========================

ATR_FLOOR_PCT = 0.0015   # dynamic floor
MIN_BARS_AFTER_OPEN = 5


# =========================
# Data Structure
# =========================

@dataclass
class MarketState:
    close: float
    high_20_prev: float
    atr: float
    vwap: float
    volume_spike: float
    regime: str
    bars_since_open: int


# =========================
# Core Calculation
# =========================

def compute_breakout_strength(close, high_20_prev, atr):
    atr_floor = max(atr, close * ATR_FLOOR_PCT)
    return (close - high_20_prev) / (atr_floor + 1e-6)


# =========================
# Regime Threshold
# =========================

def get_threshold(regime: str):
    if regime == "SQUEEZE":
        return 0.25
    elif regime == "TREND":
        return 0.15
    elif regime == "WEAK":
        return 0.20
    elif regime == "CHOP":
        return None
    return 0.25


# =========================
# Entry Logic
# =========================

def evaluate_entry(state: MarketState):
    # Session stability
    if state.bars_since_open < MIN_BARS_AFTER_OPEN:
        return None

    # Structure check
    if state.close <= state.high_20_prev:
        return None

    # Behavior check
    if not (state.volume_spike >= 1.5 and state.close > state.vwap):
        return None

    threshold = get_threshold(state.regime)
    if threshold is None:
        return None

    breakout_strength = compute_breakout_strength(
        state.close, state.high_20_prev, state.atr
    )

    # =========================
    # Entry Decisions
    # =========================

    # Confirmed breakout
    if breakout_strength >= 0.25:
        return {
            "action": "BUY",
            "size": 1.0,
            "tag": "CONFIRMED_BREAKOUT",
            "strength": breakout_strength,
        }

    # Early breakout (TREND only)
    if breakout_strength >= 0.15 and state.regime == "TREND":
        return {
            "action": "BUY",
            "size": 0.3,
            "tag": "EARLY_BREAKOUT",
            "strength": breakout_strength,
        }

    return None


# =========================
# Exit Logic (basic)
# =========================

def evaluate_exit(entry_price, current_price, atr):
    """
    Simple ATR trailing exit
    """
    stop_loss = entry_price - 1.5 * atr
    take_profit = entry_price + 3.0 * atr

    if current_price <= stop_loss:
        return {"action": "SELL", "reason": "STOP_LOSS"}

    if current_price >= take_profit:
        return {"action": "SELL", "reason": "TAKE_PROFIT"}

    return None


# =========================
# Strategy Wrapper
# =========================

class AdaptiveORBv2:

    def __init__(self, logger=None):
        self.logger = logger

    def on_bar(self, state: MarketState):
        entry = evaluate_entry(state)

        if self.logger:
            self.logger.info(
                f"[ORBv2] regime={state.regime} "
                f"entry={entry}"
            )

        return entry
```

