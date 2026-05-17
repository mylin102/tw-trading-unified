"""
ORB-Ambush Strategy — Fading the Breakout.
Uses ORB High/Low as structural boundaries and LRL Curvature to detect exhaustion.
Logic:
1. Structural Context: Price is outside first 30min high/low.
2. Ambush Filter: Enter SHORT at High if curvature is bending DOWN (negative).
3. Ambush Filter: Enter LONG at Low if curvature is bending UP (positive).
"""
from __future__ import annotations

import logging
from typing import Any

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext

logger = logging.getLogger(__name__)

class ORBAmbush(StrategyBase):
    @property
    def name(self) -> str:
        return "orb_ambush"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "market_regime": "mean_reversion_structural",
            "description": "Ambush Pivot: Fades ORB breakouts using LRL Curvature exhaustion.",
            "indicators": ["kalman", "linreg", "atr"],
        }

    def init(self, context: StrategyContext) -> None:
        params = context.config.get("params", {})
        self.range_bars = params.get("range_bars", 6)
        self.atr_mult = params.get("atr_mult", 2.0)
        self.curve_threshold = params.get("curve_threshold", 0.005) # Lower threshold for ambush
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
        
        # Divergence Indicators
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

        # 2. Get Kalman Velocity
        if len(df) < 3:
            self._set_eval(skip_reason="INSUFFICIENT_DATA", bars=len(df))
            return None
        k_series = df["kalman_close"] if "kalman_close" in df.columns else df["Close"]
        velocity = (k_series.iloc[-1] - k_series.iloc[-2]) / k_series.iloc[-1]

        # 3. Ambush Logic (Fading the Breakout)
        if context.position.size == 0 and not self._signaled:
            # AMBUSH SHORT: Price > High BUT Curvature is bending DOWN (exhaustion)
            if close > self._range_high and curve < -self.curve_threshold and velocity < 0:
                self._signaled = True
                self._set_eval(triggered=True, action="SELL", reason="AMBUSH_FAKE_UP")
                return Signal("SELL", "AMBUSH_FAKE_UP", 
                            stop_loss=close + atr * self.atr_mult,
                            target=self._range_low, # Target the other side of the range
                            confidence=0.75)
            
            # AMBUSH LONG: Price < Low BUT Curvature is bending UP
            elif close < self._range_low and curve > self.curve_threshold and velocity > 0:
                self._signaled = True
                self._set_eval(triggered=True, action="BUY", reason="AMBUSH_FAKE_DOWN")
                return Signal("BUY", "AMBUSH_FAKE_DOWN",
                            stop_loss=close - atr * self.atr_mult,
                            target=self._range_high,
                            confidence=0.75)
            else:
                self._set_eval(skip_reason="NO_AMBUSH", close=close, range=[self._range_low, self._range_high], curve=curve, velocity=velocity)

        # 4. Trailing / Dynamic Exit
        elif context.position.size > 0 and curve < -self.curve_threshold:
            self._set_eval(triggered=True, action="EXIT", reason="AMBUSH_EXIT")
            return Signal("EXIT", "AMBUSH_EXIT", stop_loss=0)
        elif context.position.size < 0 and curve > self.curve_threshold:
            self._set_eval(triggered=True, action="EXIT", reason="AMBUSH_EXIT")
            return Signal("EXIT", "AMBUSH_EXIT", stop_loss=0)
        else:
            self._set_eval(skip_reason="ALREADY_SIGNALED" if self._signaled else "POSITION_OPEN")

        return None

    def cleanup(self) -> None:
        self._reset_state()
