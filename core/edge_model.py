"""
Edge Model — Evaluates the expected advantage of a trade before entry.
Implements "Decision Intelligence" by filtering out low-quality trades.

Instead of blind execution, the system asks: "Does this trade have an edge in the current regime?"
"""
from __future__ import annotations
import logging
import numpy as np

class EdgeModel:
    def __init__(self):
        self.logger = logging.getLogger("EdgeModel")
        
    def evaluate(self, signal_score: float, context: dict, strategy_name: str) -> dict:
        """
        Evaluate the trade edge based on current market context and signal strength.
        
        Args:
            signal_score: The absolute score of the signal (0-100)
            context: dict containing:
                - momentum: current momentum value
                - regime: STRONG, WEAK, or NORMAL
                - volatility: current ATR or volatility measure
                - vwap_dist: distance from VWAP in points
            strategy_name: Name of the strategy (counter_vwap, v2_squeeze, etc.)
            
        Returns:
            dict with 'has_edge', 'edge_score', and 'reason'
        """
        momentum = abs(context.get("momentum", 0))
        regime = context.get("regime", "NORMAL")
        vwap_dist = abs(context.get("vwap_dist", 0))
        
        # Base edge calculation
        edge_score = 0.5  # Neutral start
        
        # Factor 1: Signal Strength
        if signal_score >= 80:
            edge_score += 0.2
        elif signal_score < 30:
            edge_score -= 0.2
            
        # Factor 2: Regime vs Strategy Fit
        if strategy_name == "counter_vwap":
            # Counter-VWAP thrives in WEAK/NORMAL regimes with high VWAP distance
            if regime in ["WEAK", "NORMAL"]:
                edge_score += 0.1
            elif regime == "STRONG":
                edge_score -= 0.3 # High risk of being run over in strong trend
                
            if vwap_dist > 50: # Significant deviation favors mean reversion
                edge_score += 0.1
        
        elif "squeeze" in strategy_name:
            # Squeeze strategies thrive in STRONG trend regimes
            if regime == "STRONG":
                edge_score += 0.2
            elif regime == "WEAK":
                edge_score -= 0.2 # Avoid fake-outs in weak sideways markets
                
        # Factor 3: Momentum Confirmation
        if momentum > 40:
            edge_score += 0.1
        elif momentum < 10:
            edge_score -= 0.2
            
        # Final Decision
        # Threshold 0.6: We only take trades where we have a positive bias
        has_edge = edge_score >= 0.6
        
        reason = f"Edge={edge_score:.2f} (Score={signal_score:.0f}, Regime={regime}, Mom={momentum:.0f})"
        
        return {
            "has_edge": has_edge,
            "edge_score": edge_score,
            "reason": reason if has_edge else f"LOW_EDGE: {reason}"
        }

# Global instance
edge_model = EdgeModel()
