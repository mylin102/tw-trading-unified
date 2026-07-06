"""
LR Momentum Strategy — Accelerating trends for FOP.
Uses Linear Regression Slope (Velocity) and Curvature (Acceleration)
to enter trends only when they are gaining strength.
"""
from __future__ import annotations

import logging
from typing import Any

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext

logger = logging.getLogger(__name__)

class LRMomentum(StrategyBase):
    """
    Strategy Logic:
    1. Monitor lr_slope (Velocity) and lr_curve (Acceleration).
    2. Enter LONG if slope > 0 AND curve > 0 (Upward acceleration).
    3. Enter SHORT if slope < 0 AND curve < 0 (Downward acceleration).
    4. Exit if curve reverses (Signaling trend exhaustion).
    """

    @property
    def name(self) -> str:
        return "lr_momentum"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "market_regime": "momentum",
            "description": "Bending Momentum: Enters only on trend acceleration using LRL Curvature.",
            "indicators": ["linreg", "atr"],
        }

    def init(self, context: StrategyContext) -> None:
        params = context.config.get("params", {})
        self.slope_threshold = params.get("slope_threshold", 0.5) 
        self.curve_threshold = params.get("curve_threshold", 0.1)
        self.atr_mult = params.get("atr_mult", 2.0)
        logger.info(f"LRMomentum v1.0 Initialized")

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        close = bar.get("Close", 0.0)
        slope = bar.get("lr_slope", 0.0)
        curve = bar.get("lr_curve", 0.0)
        atr = bar.get("atr", 50.0)
        
        # Entry Logic: Accelerating Strength
        if context.position.size == 0:
            if slope > self.slope_threshold and curve > self.curve_threshold:
                self._set_eval(triggered=True, action="BUY", reason="LR_ACCEL_UP")
                return Signal(
                    "BUY", "LR_ACCEL_UP", 
                    stop_loss=close - atr * self.atr_mult, 
                    target=close + atr * self.atr_mult * 3, 
                    confidence=0.75
                )
            elif slope < -self.slope_threshold and curve < -self.curve_threshold:
                self._set_eval(triggered=True, action="SELL", reason="LR_ACCEL_DOWN")
                return Signal(
                    "SELL", "LR_ACCEL_DOWN", 
                    stop_loss=close + atr * self.atr_mult, 
                    target=close - atr * self.atr_mult * 3, 
                    confidence=0.75
                )
            else:
                self._set_eval(skip_reason="NO_ACCEL", slope=slope, curve=curve)
        
        # Exit Logic: Deceleration / Bending Back
        elif context.position.size > 0 and curve < -self.curve_threshold:
            self._set_eval(triggered=True, action="EXIT", reason="LR_DECEL")
            return Signal(action="EXIT", reason="LR_DECEL", stop_loss=0)
        elif context.position.size < 0 and curve > self.curve_threshold:
            self._set_eval(triggered=True, action="EXIT", reason="LR_DECEL")
            return Signal(action="EXIT", reason="LR_DECEL", stop_loss=0)
        else:
            self._set_eval(skip_reason="POSITION_OPEN", curve=curve)

        return None

    def cleanup(self) -> None:
        logger.info("LRMomentum shutting down...")
