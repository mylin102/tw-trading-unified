"""
ORB-ML Strategy v3 — Clean Physics Model.
Kalman Filter DISABLED based on ablation study (minimal contribution vs complexity).
Uses Random Forest (v3) with Gap % + Linear Regression Curvature.
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

logger = logging.getLogger(__name__)

class ORBML(StrategyBase):
    @property
    def name(self) -> str:
        return "orb_ml"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "3.0-AI",
            "market_regime": "momentum_ml_clean",
            "description": "Clean AI ORB: Pure Physics + Gap % (No-Kalman).",
            "indicators": ["linreg", "atr"],
        }

    def init(self, context: StrategyContext) -> None:
        params = context.config.get("params", {})
        self.prob_threshold = params.get("prob_threshold", 0.65)
        self.atr_mult = params.get("atr_mult", 2.0)
        
        # GSD: Anchor model path - LOADING V3 (Clean No-Kalman)
        project_root = Path(__file__).parents[3]
        model_path = project_root / "models" / "orb_rf_v3_clean.pkl"
        
        if model_path.exists():
            with open(model_path, "rb") as f:
                self.model = pickle.load(f)
            logger.info(f"ORBML v3: Model loaded from {model_path}")
        else:
            self.model = None
            logger.error(f"ORBML: Model file NOT FOUND at {model_path}!")

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
        if not bar or self.model is None: return None

        # ═══ df_5m guard — prevent NoneType crash when data not ready ═══
        df = context.market.df_5m
        if df is None or df.empty:
            logger.debug("orb_ml: df_5m None/empty — skipping")
            return None

        # Session Reset & Gap Calculation
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

        # 2. Potential Breakout Detected
        close = bar['Close']
        direction = 0
        if close > self._range_high: direction = 1
        elif close < self._range_low: direction = -1

        if direction != 0 and not self._signaled and context.position.size == 0:
            # 3. ML Inference (V3: dir, lr_curve, atr_n, gap_p, hour)
            features = pd.DataFrame([{
                "dir": direction,
                "lr_curve": bar.get("lr_curve", 0.0),
                "atr_n": bar.get("atr", 50.0) / close,
                "gap_p": self._gap_p,
                "hour": df.index[-1].hour
            }])
            
            probs = self.model.predict_proba(features)[0]
            success_prob = probs[1]
            
            if success_prob >= self.prob_threshold:
                qty = 1
                if success_prob >= 0.85: qty = 3
                elif success_prob >= 0.75: qty = 2

                self._signaled = True
                logger.info(f"🤖 AI V3 (Clean) ENTRY: Prob={success_prob:.2%}, Qty={qty}")
                return Signal(
                    "BUY" if direction == 1 else "SELL",
                    f"AI_ORB_V3_{'UP' if direction == 1 else 'DOWN'}",
                    stop_loss=close - direction * (bar.get("atr", 50.0) * self.atr_mult),
                    target=close + direction * (bar.get("atr", 50.0) * self.atr_mult * 2),
                    confidence=success_prob,
                    quantity=qty
                )

        return None

    def cleanup(self) -> None:
        self._reset_state()
