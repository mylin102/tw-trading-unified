"""
Exit Engine — Replaces rigid EOD and fixed stops with context-aware logic.
"""
from __future__ import annotations
from datetime import datetime
from core.edge_model import edge_model
from core.risk import dynamic_stop_loss

def should_exit(trade_state: dict, context: dict, market: dict) -> tuple[bool, str]:
    """
    Evaluate if a position should be closed.
    
    Args:
        trade_state: {entry_price, side, peak_price, position_age_bars}
        context: {regime, momentum, volatility, vwap_dist, signal_score}
        market: {current_price, atr, time_to_close_mins}
        
    Returns:
        (bool, reason)
    """
    # 1. Edge Evaluation
    edge_res = edge_model.evaluate(context.get("signal_score", 50), context, "exit_check")
    edge = edge_res["edge_score"]
    
    # --- 1.1 Edge Decay ---
    if edge < 0.3:
        return True, f"EXIT_NO_EDGE ({edge:.2f})"

    # 2. Dynamic Adaptive Stop Loss
    regime_dict = {
        "volatility": context.get("volatility_norm", 0.5),
        "trend_strength": 0.8 if context.get("regime") == "STRONG" else 0.4
    }
    
    stop_price = dynamic_stop_loss(
        trade_state["entry_price"],
        market["atr"],
        regime_dict,
        edge=edge,
        side=trade_state["side"]
    )
    
    curr_p = market["price"]
    side = trade_state["side"]
    
    if side == "LONG" and curr_p <= stop_price:
        return True, f"ADAPTIVE_SL ({curr_p:.1f} <= {stop_price:.1f})"
    elif side == "SHORT" and curr_p >= stop_price:
        return True, f"ADAPTIVE_SL ({curr_p:.1f} >= {stop_price:.1f})"

    # 3. EOD Optimization (Replaces fixed 13:25)
    # If close to market close, we exit unless we have a very strong edge
    if market["time_to_close_mins"] < 10:
        if edge < 0.6:
            return True, f"EOD_WEAK_EDGE ({edge:.2f})"
        # If edge is > 0.6, we might 'HOLD' through the close (if policy allows overnight)
        # For now, let's keep it safe and return HOLD only if edge is stellar
        if edge < 0.8:
             return True, f"EOD_FINAL_SETTLE"

    return False, "HOLD"
