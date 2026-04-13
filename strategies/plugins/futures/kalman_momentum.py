"""
Kalman Momentum Strategy — Denoised trend following with Squeeze filter.
Uses Kalman Filter + Squeeze fire window to reduce false signals.
"""
from __future__ import annotations

import logging
from typing import Any

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext

logger = logging.getLogger(__name__)

class KalmanMomentum(StrategyBase):
    """
    Strategy Logic:
    1. Monitor Squeeze status (fired).
    2. Open a 5-bar window after Squeeze fire.
    3. Enter if Kalman velocity > sensitivity within the window.
    """

    @property
    def name(self) -> str:
        return "kalman_momentum"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.3",
            "market_regime": "trending",
            "description": "Denoised Momentum: Kalman Filter + Squeeze Guard.",
            "indicators": ["kalman", "atr", "squeeze"],
        }

    def init(self, context: StrategyContext) -> None:
        params = context.config.get("params", {})
        self.sensitivity = params.get("sensitivity", 0.00001) 
        self.atr_mult = params.get("atr_mult", 2.0)
        self.kalman_q = params.get("kalman_q", 1e-4)
        self.kalman_r = params.get("kalman_r", 0.01)
        self.window = params.get("window", 5)
        self._fired_timer = 0
        logger.info(f"KalmanMomentum v1.3 Initialized")

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        close = bar.get("Close", 0.0)
        k_close = bar.get("kalman_close", close)
        atr = bar.get("atr", 50.0)
        fired = bar.get("fired", False)
        
        # Window Management
        if fired:
            self._fired_timer = self.window
        elif self._fired_timer > 0:
            self._fired_timer -= 1
            
        df = context.market.df_5m
        if len(df) < 3: return None
            
        k_series = df["kalman_close"] if "kalman_close" in df.columns else df["Close"]
        velocity = (k_series.iloc[-1] - k_series.iloc[-2]) / k_series.iloc[-1]
        
        # Entry Logic: Window + Denoised Trend
        if context.position.size == 0:
            if self._fired_timer > 0:
                if velocity > self.sensitivity:
                    self._fired_timer = 0
                    return Signal("BUY", "KALMAN_SQZ_UP", stop_loss=close - atr * self.atr_mult, target=close + atr * self.atr_mult * 2, confidence=0.8)
                elif velocity < -self.sensitivity:
                    self._fired_timer = 0
                    return Signal("SELL", "KALMAN_SQZ_DOWN", stop_loss=close + atr * self.atr_mult, target=close - atr * self.atr_mult * 2, confidence=0.8)
        
        # Exit: Trend Reversal (Optional but recommended for Kalman)
        elif context.position.size > 0 and velocity < -0.000001:
            return Signal(action="EXIT", reason="KALMAN_FLIP", stop_loss=0)
        elif context.position.size < 0 and velocity > 0.000001:
            return Signal(action="EXIT", reason="KALMAN_FLIP", stop_loss=0)

        return None

    def cleanup(self) -> None:
        logger.info("KalmanMomentum shutting down...")
