"""
ORB-Advanced Strategy — The Winning Confluence.
Combines Opening Range Breakout (ORB) with Kalman Denoising and LRL Curvature.
Logic:
1. Structural Trigger: Breakout of first 30min high/low.
2. Entry Filter: Kalman velocity must match direction; LRL Curvature must be accelerating.
3. Exit Filter: LRL Curvature reversal (Trend Exhaustion) or ATR trailing stop.
"""
from __future__ import annotations

import logging
from typing import Any

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext

logger = logging.getLogger(__name__)

class ORBAdvanced(StrategyBase):
    @property
    def name(self) -> str:
        return "orb_advanced"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "market_regime": "momentum_structural",
            "description": "Elite Confluence: ORB + Kalman + LRL Curvature.",
            "indicators": ["kalman", "linreg", "atr"],
        }

    def init(self, context: StrategyContext) -> None:
        params = context.config.get("params", {})
        self.range_bars = params.get("range_bars", 6) # First 30 mins
        self.atr_mult = params.get("atr_mult", 2.5)
        self.sens = params.get("sensitivity", 0.00001)
        self.curve_threshold = params.get("curve_threshold", 0.01)
        self._reset_state()

    def _reset_state(self):
        self._range_high = 0.0
        self._range_low = float('inf')
        self._bar_count = 0
        self._range_built = False
        self._signaled = False
        self._last_session = None

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        # ═══ df_5m guard — prevent NoneType crash when data not ready ═══
        df = context.market.df_5m
        if df is None or df.empty:
            self._set_eval(skip_reason="NO_DATA")
            return None

        close = bar.get("Close", 0.0)
        high = bar.get("High", 0.0)
        low = bar.get("Low", 0.0)
        atr = bar.get("atr", 50.0)
        
        # New Confluence Indicators
        k_close = bar.get("kalman_close", close)
        slope = bar.get("lr_slope", 0.0)
        curve = bar.get("lr_curve", 0.0)
        
        # Session Reset Logic
        session_id = bar.get("trading_day", bar.get("session", 1))
        if self._last_session != session_id:
            self._reset_state()
            self._last_session = session_id

        # 1. Build ORB Range
        if not self._range_built:
            self._range_high = max(self._range_high, high)
            self._range_low = min(self._range_low, low)
            self._bar_count += 1
            if self._bar_count >= self.range_bars:
                self._range_built = True
            self._set_eval(skip_reason="ORB_BUILDING", bars=self._bar_count)
            return None

        # 2. Get Velocity from Kalman
        if len(df) < 3:
            self._set_eval(skip_reason="INSUFFICIENT_DATA", bars=len(df))
            return None
        k_series = df["kalman_close"] if "kalman_close" in df.columns else df["Close"]
        velocity = (k_series.iloc[-1] - k_series.iloc[-2]) / k_series.iloc[-1]

        # 3. Entry Logic (Structural + Acceleration)
        if context.position.size == 0 and not self._signaled:
            # LONG: Price > Range AND Kalman rising AND Curvature accelerating
            if close > self._range_high and velocity > self.sens and curve > self.curve_threshold:
                self._signaled = True
                self._set_eval(triggered=True, action="BUY", reason="ORB_ADV_LONG")
                return Signal("BUY", "ORB_ADV_LONG", 
                            stop_loss=close - atr * self.atr_mult,
                            target=close + (self._range_high - self._range_low) * 2,
                            confidence=0.85)
            
            # SHORT: Price < Range AND Kalman falling AND Curvature accelerating down
            elif close < self._range_low and velocity < -self.sens and curve < -self.curve_threshold:
                self._signaled = True
                self._set_eval(triggered=True, action="SELL", reason="ORB_ADV_SHORT")
                return Signal("SELL", "ORB_ADV_SHORT",
                            stop_loss=close + atr * self.atr_mult,
                            target=close - (self._range_high - self._range_low) * 2,
                            confidence=0.85)
            else:
                self._set_eval(skip_reason="NO_CONFLUENCE", close=close, range=[self._range_low, self._range_high], velocity=velocity, curve=curve)

        # 4. Exhaustion Exit (Physics-based)
        elif context.position.size > 0 and curve < -self.curve_threshold:
            self._set_eval(triggered=True, action="EXIT", reason="TREND_EXHAUSTION")
            return Signal("EXIT", "TREND_EXHAUSTION", stop_loss=0)
        elif context.position.size < 0 and curve > self.curve_threshold:
            self._set_eval(triggered=True, action="EXIT", reason="TREND_EXHAUSTION")
            return Signal("EXIT", "TREND_EXHAUSTION", stop_loss=0)
        else:
            self._set_eval(skip_reason="ALREADY_SIGNALED" if self._signaled else "POSITION_OPEN")

        return None

    def cleanup(self) -> None:
        self._reset_state()
