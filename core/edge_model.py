"""
Edge Model V2 — Standardized Scoring with Exploration (Bandit approach).
Ensures feature scaling doesn't destroy the score and allows exploration of rejected signals.
"""
from __future__ import annotations
import logging
import random

class EdgeModel:
    def __init__(self):
        self.logger = logging.getLogger("EdgeModel")
        # [GSD Force Calibration] Set high thresholds to filter noise
        self.thresholds = {
            "counter_vwap": 0.65, 
            "orb_breakout": 0.60,
            "default": 0.65
        }
        self._ml_model = None
        self._scaler = None
        self._load_ml_model()
        
    def _load_ml_model(self):
        """Try to load pre-trained Logistic Regression model."""
        import joblib
        from pathlib import Path
        model_path = Path(__file__).resolve().parent.parent / "models" / "edge_lr.pkl"
        scaler_path = Path(__file__).resolve().parent.parent / "models" / "scaler.pkl"
        
        if model_path.exists() and scaler_path.exists():
            try:
                self._ml_model = joblib.load(model_path)
                self._scaler = joblib.load(scaler_path)
                self.logger.info("🧠 ML Edge Model (Logistic Regression) loaded.")
            except Exception as e:
                self.logger.warning(f"⚠️ Failed to load ML model: {e}")

    def compute_edge(self, features: dict) -> float:
        """
        Compute edge score using Top 3 Alpha features only (Selection).
        """
        if self._ml_model and self._scaler:
            try:
                # Use ONLY the most predictive features
                vec = np.array([[
                    features.get("trend_strength", 0.5),
                    features.get("volatility", 0.5),
                    features.get("signal_strength", 0.5),
                    features.get("vwap_distance", 0.0),
                    features.get("momentum_norm", 0.0),
                    features.get("breakout_strength", 0.0),
                    features.get("volume_spike", 1.0),
                    features.get("trend_strength_raw", 0.0)
                ]])
                X_scaled = self._scaler.transform(vec)
                return float(self._ml_model.predict_proba(X_scaled)[0, 1])
            except Exception: pass

        # RULE-BASED: Focus on Interaction Alpha
        score = 0.5
        # High Breakout + High Volume = High Edge
        if features.get("breakout_strength", 0) > 1.5 and features.get("volume_spike", 0) > 1.2:
            score += 0.2
        # Low Strength + Mean Reversion Context = Edge for Counter
        if abs(features.get("trend_strength_raw", 0)) < 0.001 and abs(features.get("vwap_distance", 0)) > 0.5:
            score += 0.1
            
        return max(0.0, min(1.0, score))

    def evaluate(self, signal_score: float, context: dict, strategy_name: str) -> dict:
        """Evaluate with Soft Allocation (Expectancy-driven)."""
        # Standardize features
        price = context.get("price", 20000)
        atr = context.get("volatility", 50)
        vol_norm = min(1.0, atr / (price * 0.05)) if price > 0 else 0.5
        vwap_dist_raw = context.get("vwap_dist", 0)
        vwap_norm = min(1.0, vwap_dist_raw / 100.0)
        
        regime = context.get("regime", "NORMAL")
        trend_raw = float(context.get("trend_strength_raw", 0))

        features = {
            "trend_strength": 0.8 if regime == "STRONG" else 0.4,
            "volatility": vol_norm,
            "signal_strength": min(1.0, signal_score / 100.0),
            "vwap_distance": vwap_norm,
            "momentum_norm": float(context.get("momentum", 0)) / 100.0,
            "breakout_strength": float(context.get("breakout_strength", 0)),
            "volume_spike": float(context.get("volume_spike", 1.0)),
            "trend_strength_raw": trend_raw
        }
        
        # --- DIRECTIONAL SHIELD ---
        shield_blocked = False
        side = context.get("side", "UNKNOWN")
        if regime == "STRONG":
            if trend_raw > 0.002 and side == "SHORT": shield_blocked = True
            if trend_raw < -0.002 and side == "LONG": shield_blocked = True

        if shield_blocked:
            return {
                "has_edge": False, 
                "edge_score": 0.0, 
                "pos_scale": 0.0, 
                "rank": "SHIELD_BLOCKED", 
                "is_exploring": False,
                "reason": "Shield Blocked",
                "features": features
            }

        prob = self.compute_edge(features)
        
        # --- SOFT ALLOCATION (GSD 4.7) ---
        # Instead of 'if prob > threshold', we use: size = max(0, (prob - base) * multiplier)
        # Calibration from previous results:
        base_configs = {
            "counter_vwap": {"base": 0.40, "mult": 2.0},
            "orb_breakout": {"base": 0.45, "mult": 3.0},
            "default": {"base": 0.50, "mult": 2.0}
        }
        cfg = base_configs.get(strategy_name, base_configs["default"])
        
        # Linear Ramp Function
        pos_scale = max(0.0, (prob - cfg["base"]) * cfg["mult"])
        pos_scale = min(2.0, pos_scale) # Cap at 2x base size
        
        # Classification for UX
        if pos_scale >= 1.2: rank = "ALPHA"
        elif pos_scale >= 0.7: rank = "BETA"
        elif pos_scale > 0: rank = "GAMMA"
        else: rank = "NO_EDGE"
            
        has_edge = pos_scale > 0.1 # Minimum viable size
        is_exploring = (prob < cfg["base"]) and pos_scale > 0

        source = "ML" if self._ml_model else "RULE"
        reason = f"Rank={rank}, Prob={prob:.2f}, Scale={pos_scale:.1f}"
        
        return {
            "has_edge": has_edge,
            "edge_score": prob,
            "pos_scale": pos_scale,
            "rank": rank,
            "is_exploring": is_exploring,
            "reason": reason,
            "features": features
        }

# Global instance
edge_model = EdgeModel()
