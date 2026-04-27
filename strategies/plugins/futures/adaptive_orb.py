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
        params = {
            **context.config.get("strategy", {}).get("adaptive_orb", {}),
            **context.config.get("params", {}),
        }
        self.atr_mult = params.get("atr_mult", 2.0)
        self.prob_threshold = params.get("prob_threshold", 0.65)
        self.min_volume_multiple = params.get("min_volume_multiple", 1.2)
        self.vwap_confirm_bars = params.get("vwap_confirm_bars", 3)
        self.min_price_vs_vwap = params.get("min_price_vs_vwap", 0.0)
        self.min_curve_abs = params.get("min_curve_abs", 0.01)
        
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

    @staticmethod
    def _bar_volume(bar: dict[str, Any]) -> float:
        return float(bar.get("Volume", bar.get("volume", 0.0)))

    def _has_volume_confirmation(self, df: pd.DataFrame | None, bar: dict[str, Any]) -> bool:
        current_volume = self._bar_volume(bar)
        if current_volume <= 0:
            return False
        if df is None or len(df) < 2:
            return False

        volume_col = "Volume" if "Volume" in df.columns else ("volume" if "volume" in df.columns else None)
        if volume_col is None:
            return False

        recent = df[volume_col].iloc[:-1].tail(5)
        if recent.empty:
            return False

        baseline = recent.mean()
        if baseline <= 0:
            return current_volume > 0

        return current_volume >= baseline * self.min_volume_multiple

    def _has_vwap_trend_support(self, df: pd.DataFrame | None, direction: int) -> bool:
        if df is None or len(df) < self.vwap_confirm_bars:
            return False
        if "vwap" not in df.columns or "Close" not in df.columns:
            return False

        recent = df.iloc[-self.vwap_confirm_bars:]
        closes = recent["Close"]
        vwap = recent["vwap"]
        if closes.isna().any() or vwap.isna().any():
            return False

        vwap_delta = vwap.diff().dropna()
        if vwap_delta.empty:
            return False

        if direction == 1:
            if not (closes >= vwap).all():
                return False
            if not (vwap_delta > 0).all():
                return False
            return float((closes.iloc[-1] - vwap.iloc[-1]) / vwap.iloc[-1]) >= self.min_price_vs_vwap

        if not (closes <= vwap).all():
            return False
        if not (vwap_delta < 0).all():
            return False
        return float((closes.iloc[-1] - vwap.iloc[-1]) / vwap.iloc[-1]) <= -self.min_price_vs_vwap

    def _has_breakout_quality(self, context: StrategyContext, direction: int) -> bool:
        bar = context.market.last_bar
        curve = float(bar.get("lr_curve", 0.0))
        if direction == 1 and curve < self.min_curve_abs:
            return False
        if direction == -1 and curve > -self.min_curve_abs:
            return False
        if not self._has_volume_confirmation(context.market.df_5m, bar):
            return False
        if not self._has_vwap_trend_support(context.market.df_5m, direction):
            return False
        return True

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        regime = context.market.regime

        # ═══ df_5m guard — prevent NoneType crash when data not ready ═══
        df = context.market.df_5m
        if df is None or df.empty:
            logger.debug("adaptive_orb: df_5m None/empty — skipping")
            return None

        if not bar: return None

        # Session Reset
        session_id = bar.get("trading_day", bar.get("session", 1))
        if self._last_session != session_id:
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
            if not self._has_breakout_quality(context, direction):
                return None
            # V3 Features: dir, lr_curve, atr_n, gap_p, hour
            features = pd.DataFrame([{
                "dir": direction,
                "lr_curve": curve,
                "atr_n": atr / close,
                "gap_p": self._gap_p,
                "hour": df.index[-1].hour
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
