"""
Adaptive ORB Strategy v3 — Clean AI Edition.
Dynamically switches between ML-Breakout and Ambush Fader.
Optimized for V3 Clean Model (No-Kalman).
"""
from __future__ import annotations
import logging
import pickle
from pathlib import Path
from typing import Any

import pandas as pd
from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.market_regime import MarketRegime

logger = logging.getLogger(__name__)

class AdaptiveORB(StrategyBase):
    @property
    def name(self) -> str:
        return "adaptive_orb"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "3.0-AI",
            "market_regime": "adaptive_ml_clean",
            "description": "Switching Engine V3: ML-Breakout (Clean) vs Ambush.",
            "indicators": ["linreg", "atr"],
        }

    def init(self, context: StrategyContext) -> None:
        params = context.config.get("params", {})
        self.atr_mult = params.get("atr_mult", 2.0)
        self.prob_threshold = params.get("prob_threshold", 0.65)
        
        # GSD: Load V3 Clean Model
        project_root = Path(__file__).parents[3]
        model_path = project_root / "models" / "orb_rf_v3_clean.pkl"
        
        if model_path.exists():
            with open(model_path, "rb") as f:
                self.model = pickle.load(f)
            logger.info(f"AdaptiveORB v3: Model loaded.")
        else:
            self.model = None
            logger.error(f"AdaptiveORB: Model NOT FOUND at {model_path}!")

        self._reset_state()

    def _reset_state(self):
        self._range_high = 0.0
        self._range_low = float('inf')
        self._bar_count = 0
        self._range_built = False
        self._signaled = False
        self._last_session = None
        self._gap_p = 0.0

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        regime = context.market.regime
        if not bar: return None

        # Session Reset
        session_id = bar.get("trading_day", bar.get("session", 1))
        if self._last_session != session_id:
            df = context.market.df_5m
            if len(df) >= 2:
                prev_close = df.iloc[-2]['Close']
                self._gap_p = (bar['Open'] - prev_close) / prev_close
            self._reset_state()
            self._last_session = session_id

        # 1. Build ORB
        if not self._range_built:
            self._range_high = max(self._range_high, bar['High'])
            self._range_low = min(self._range_low, bar['Low'])
            self._bar_count += 1
            if self._bar_count >= 6: self._range_built = True
            return None

        if self._signaled or context.position.size != 0: return None

        close = bar['Close']
        atr = bar.get("atr", 50.0)
        curve = bar.get("lr_curve", 0.0)

        # 2. Breakout Detection
        direction = 0
        if close > self._range_high: direction = 1
        elif close < self._range_low: direction = -1
        
        if direction == 0: return None

        # 3. Adaptive Logic (V3 Clean)
        if regime == MarketRegime.TRENDING and self.model:
            # V3 Features: dir, lr_curve, atr_n, gap_p, hour
            features = pd.DataFrame([{
                "dir": direction,
                "lr_curve": curve,
                "atr_n": atr / close,
                "gap_p": self._gap_p,
                "hour": context.market.df_5m.index[-1].hour
            }])
            success_prob = self.model.predict_proba(features)[0][1]
            
            if success_prob >= self.prob_threshold:
                self._signaled = True
                qty = 3 if success_prob >= 0.85 else (2 if success_prob >= 0.75 else 1)
                return Signal("BUY" if direction == 1 else "SELL", "ADAPTIVE_TREND_V3",
                            stop_loss=close - direction * (atr * self.atr_mult),
                            target=close + direction * (atr * self.atr_mult * 3),
                            confidence=success_prob, quantity=qty)

        elif regime == MarketRegime.CHOPPY:
            # Ambush Logic
            if (direction == 1 and curve < -0.01) or (direction == -1 and curve > 0.01):
                self._signaled = True
                return Signal("SELL" if direction == 1 else "BUY", "ADAPTIVE_AMBUSH",
                            stop_loss=close + direction * (atr * 1.5),
                            target=self._range_low if direction == 1 else self._range_high,
                            confidence=0.7, quantity=1)

        return None

    def cleanup(self) -> None:
        self._reset_state()
