"""
Risk Management Module — Adaptive stop-loss and position sizing.
"""
from __future__ import annotations

def dynamic_stop_loss(entry_price: float, atr: float, regime: dict, edge: float | None = None, side: str = "LONG") -> float:
    """
    Calculate an adaptive stop loss based on volatility (ATR), market regime, and edge.
    
    Args:
        entry_price: Price at entry
        atr: Current Average True Range
        regime: dict with 'volatility' (0-1) and 'trend_strength' (0-1)
        edge: Edge score from edge_model (0-1)
        side: "LONG" or "SHORT"
    """
    # Base multiplier
    k = 2.0 

    # --- Regime Adjustment ---
    vol = regime.get("volatility", 0.5)
    trend = regime.get("trend_strength", 0.5)
    
    if vol > 0.7:
        k *= 1.3   # High Volatility -> Wider stop
    elif vol < 0.3:
        k *= 0.8   # Low Volatility -> Tighter stop

    if trend > 0.6:
        k *= 1.2   # Strong Trend -> Give more room to run

    # --- Edge Adjustment (Decision Intelligence) ---
    if edge is not None:
        if edge > 0.7:
            k *= 1.2   # High Edge -> High conviction, allow more breathing room
        elif edge < 0.4:
            k *= 0.7   # Low Edge -> Low conviction, cut early

    stop_distance = k * atr
    
    if side == "SHORT":
        return entry_price + stop_distance
    return entry_price - stop_distance
