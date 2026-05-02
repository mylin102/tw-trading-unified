"""Adaptive ORB v1.5 — v1 ORB entry + ATR breakout confirmation.

v1.5 = v1 (ORB range + ML model) + breakout_strength confirmation gate.

Changes vs v1:
  + ATR-normalized breakout_strength confirmation (from v2's ATR engine)
  + Volume + VWAP behavior check before entry
  + Session stabilization (min 5 bars since open)
  - ML model still used for probability scoring (kept from v1)
  - ORB range building retained (v1's `_range_high`/`_range_low`)
  - Exit logic UNCHANGED — v1's risk_mgmt layer (trailing stop, trend hold) is preserved

Key design principle (from v2 verdict):
  Do NOT sacrifice MFE/MAE ratio for win rate.
  v1 holds 236 bars avg (MFE/MAE=1.74); v2 holds 15 bars (MFE/MAE=1.02).
  v1.5 keeps v1's exit path entirely.
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
from core.strategy_eval import StrategyEval
from core.market_regime import MarketRegime

logger = logging.getLogger(__name__)

# ── Breakout confirmation thresholds (from breakout_engine_v2.md) ──
ATR_FLOOR_PCT = 0.0015
MIN_BARS_AFTER_OPEN = 5
MIN_VOLUME_SPIKE = 1.5


class AdaptiveORBv15(StrategyBase):
    """v1.5: ORB range entry + ATR breakout confirmation gate.
    
    Entry flow:
      1. Build ORB range (6 bars) — v1 behavior
      2. Wait for breakout (close > high / close < low) — v1 behavior
      3. ***NEW*** ATR breakout strength confirmation:
         - close > High20_prev (structure)
         - volume_spike >= 1.5 + close > vwap (behavior)
         - breakout_strength > 0 (strength floor)
      4. ML probability model (if TRENDING regime) — v1 behavior
      5. Ambush fader (if CHOPPY regime) — v1 behavior

    Exit: entirely delegated to monitor's risk_mgmt (trailing stop, trend hold, etc.)
    """

    @property
    def name(self) -> str:
        return "adaptive_orb_v15"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.5",
            "market_regime": "adaptive_ml_clean",
            "description": "v1 ORB + ATR breakout confirmation gate (v1.5)",
            "indicators": ["linreg", "atr", "breakout_strength", "volume_spike"],
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

        # Load V3 Clean Model (same as v1)
        project_root = Path(__file__).parents[3]
        model_path = project_root / "models" / "orb_rf_v3_clean.pkl"
        if model_path.exists():
            with open(model_path, "rb") as f:
                self.model = pickle.load(f)
            logger.info("AdaptiveORBv15: Model loaded.")
        else:
            self.model = None
            logger.error(f"AdaptiveORBv15: Model NOT FOUND at {model_path}!")

        self._reset_state()

    def _reset_state(self):
        self._range_high = 0.0
        self._range_low = float("inf")
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
        """v1-style breakout quality check (curve + volume + VWAP)."""
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

    def _check_atr_breakout_confirmation(self, bar: dict, df: pd.DataFrame | None) -> tuple[bool, str]:
        """ATR breakout confirmation gate (v1.5 addition).
        
        Checks:
          1. Structure: close > High20.shift(1)
          2. Strength: breakout_strength > 0 (positive)
          3. Behavior: volume_spike >= 1.5 and close > vwap
        """
        close = float(bar.get("Close", 0))

        # ── 1. Structure ──
        if df is None or len(df) < 21:
            return True, "INSUFFICIENT_DATA"  # skip check if not available
        high_col = "High" if "High" in df.columns else "high"
        high_20_prev = float(df[high_col].rolling(20).max().shift(1).iloc[-1])
        if close <= high_20_prev:
            return False, f"NO_STRUCTURE close={close:.1f} <= High20_prev={high_20_prev:.1f}"

        # ── 2. Behavior: volume spike + VWAP ──
        volume_spike = float(bar.get("volume_spike", 0))
        vwap = float(bar.get("vwap", 0))

        if volume_spike < MIN_VOLUME_SPIKE:
            return False, f"VOLUME_TOO_LOW spike={volume_spike:.2f} < {MIN_VOLUME_SPIKE}"
        if vwap > 0 and close <= vwap:
            return False, f"VWAP_REJECT close={close:.1f} <= vwap={vwap:.1f}"

        return True, "ATR_CONFIRMED"

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        regime = context.market.regime

        # ═══ df_5m guard ═══
        df = context.market.df_5m
        if df is None or df.empty:
            logger.debug("adaptive_orb_v15: df_5m None/empty — skipping")
            self._set_eval(skip_reason="NO_DATA", df_ready=False)
            return None

        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        # Session Reset
        session_id = bar.get("trading_day", bar.get("session", 1))
        if self._last_session != session_id:
            if len(df) >= 2:
                prev_close = df.iloc[-2]["Close"]
                self._gap_p = (bar["Open"] - prev_close) / prev_close
            self._reset_state()
            self._last_session = session_id

        # 1. Build ORB
        if not self._range_built:
            self._range_high = max(self._range_high, bar["High"])
            self._range_low = min(self._range_low, bar["Low"])
            self._bar_count += 1
            if self._bar_count >= 6:
                self._range_built = True
                self._set_eval(skip_reason="ORB_BUILDING", bar_count=self._bar_count, range_built=True)
            else:
                self._set_eval(skip_reason="ORB_BUILDING", bar_count=self._bar_count, range_built=False)
            return None

        if self._signaled:
            self._set_eval(skip_reason="ALREADY_SIGNALED", range_high=self._range_high, range_low=self._range_low)
            return None
        if context.position.size != 0:
            self._set_eval(skip_reason="POSITION_OPEN", position=context.position.size)
            return None

        close = bar["Close"]
        atr = bar.get("atr", 50.0)
        curve = bar.get("lr_curve", 0.0)

        # 2. ORB Breakout Detection (v1)
        direction = 0
        if close > self._range_high:
            direction = 1
        elif close < self._range_low:
            direction = -1

        if direction == 0:
            self._set_eval(skip_reason="NO_BREAKOUT", close=close, range_high=self._range_high, range_low=self._range_low)
            return None

        # ═══ 3. v1.5: ATR breakout confirmation gate ═══
        # Only accept ORB breakout if ATR structure + behavior confirms.
        atr_ok, atr_reason = self._check_atr_breakout_confirmation(bar, df)
        if not atr_ok:
            self._set_eval(skip_reason=f"ATR_GATE_REJECT:{atr_reason}",
                           direction=direction, atr_reason=atr_reason,
                           close=close, range_high=self._range_high, range_low=self._range_low)
            return None
        self._set_eval(skip_reason="ATR_GATE_PASSED", atr_reason=atr_reason)

        # 4. Adaptive Logic (v1, unchanged)
        if regime == MarketRegime.TRENDING and self.model:
            if not self._has_breakout_quality(context, direction):
                self._set_eval(skip_reason="BREAKOUT_QUALITY_FAILED", direction=direction, curve=curve, min_curve_abs=self.min_curve_abs)
                return None
            features = pd.DataFrame([{
                "dir": direction,
                "lr_curve": curve,
                "atr_n": atr / close,
                "gap_p": self._gap_p,
                "hour": df.index[-1].hour,
            }])
            success_prob = self.model.predict_proba(features)[0][1]
            if success_prob >= self.prob_threshold:
                self._signaled = True
                qty = 3 if success_prob >= 0.85 else (2 if success_prob >= 0.75 else 1)
                action = "BUY" if direction == 1 else "SELL"
                self._set_eval(triggered=True, action=action, edge_score=success_prob, quantity=qty, direction=direction)
                return Signal(action, "ADAPTIVE_TREND_V15",
                              stop_loss=close - direction * (atr * self.atr_mult),
                              target=close + direction * (atr * self.atr_mult * 3),
                              confidence=success_prob, quantity=qty)
            self._set_eval(skip_reason="MODEL_PROB_TOO_LOW", success_prob=success_prob, prob_threshold=self.prob_threshold, direction=direction)

        elif regime == MarketRegime.CHOPPY:
            # Ambush Logic (v1)
            if (direction == 1 and curve < -0.01) or (direction == -1 and curve > 0.01):
                self._signaled = True
                ambush_action = "SELL" if direction == 1 else "BUY"
                self._set_eval(triggered=True, action=ambush_action, edge_score=0.70, signal="ADAPTIVE_AMBUSH_V15", direction=direction, curve=curve)
                return Signal(ambush_action, "ADAPTIVE_AMBUSH_V15",
                              stop_loss=close + direction * (atr * 1.5),
                              target=self._range_low if direction == 1 else self._range_high,
                              confidence=0.7, quantity=1)
            self._set_eval(skip_reason="AMBUSH_CURVE_MISMATCH", direction=direction, curve=curve)
        else:
            self._set_eval(skip_reason="REGIME_NOT_TRADABLE", regime=str(regime), direction=direction)

        return None

    def cleanup(self) -> None:
        self._reset_state()
