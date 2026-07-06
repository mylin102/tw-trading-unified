"""
adaptive_orb_v2 — Opening Range Breakout with Structural Confirmation.
Upgraded to use High10/Low10 for faster structural detection.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext

logger = logging.getLogger(__name__)

# Constants
MIN_VOLUME_SPIKE = 1.5
CONFIRMED_THRESHOLD = 0.25

class AdaptiveOrbV2(StrategyBase):
    """Adaptive ORB with V2 Breakout Engine logic."""

    @property
    def name(self) -> str:
        return "adaptive_orb_v2"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "2.0",
            "market_regime": "TREND, SQUEEZE",
            "description": "Adaptive ORB with 10-bar structural confirmation",
            "indicators": ["breakout_strength_atr", "volume_spike", "vwap", "atr"],
        }

    def init(self, context: StrategyContext) -> None:
        self._last_session = None
        self._signaled = False
        self._bar_count = 0

    def _reset_state(self):
        self._signaled = False
        self._bar_count = 0

    def _get_threshold(self, regime: str) -> float | None:
        """Regime-aware breakout threshold."""
        regime = regime.upper()
        if regime == "TREND":
            return 0.15  # Aggressive in trend
        if regime == "SQUEEZE":
            return 0.25  # Standard in squeeze
        if regime == "TRANSITION":
            return 0.20  # Mid-point for transition
        return None

    def _compute_breakout_strength(self, close: float, high_prev: float, atr: float) -> float:
        """ATR-normalized distance from structural level."""
        atr_floor = max(atr, close * 0.0015)
        return (close - high_prev) / atr_floor

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        # ── Session reset ──
        session_id = bar.get("trading_day", bar.get("session", 1))
        if self._last_session != session_id:
            self._reset_state()
            self._last_session = session_id

        self._bar_count += 1

        if self._signaled:
            self._set_eval(skip_reason="ALREADY_SIGNALED")
            return None

        if context.position.size != 0:
            self._set_eval(skip_reason="POSITION_OPEN", position=context.position.size)
            return None

        # ── Opening Range Buffer ──
        # Day session: 30 mins (6 bars), Night session: 30 mins (6 bars)
        if self._bar_count <= 6:
            self._set_eval(skip_reason="ORB_BUILDING", bar_count=self._bar_count)
            return None

        # ── Data extraction ──
        df = context.market.df_5m
        close = float(bar.get("Close", 0))
        atr = float(bar.get("atr", 50))
        vwap = float(bar.get("vwap", 0))
        volume_spike = float(bar.get("volume_spike", 0))
        regime = str(getattr(context.market, "regime", "UNKNOWN")).upper()

        # ── Structure: close > High10.shift(1) ──
        if df is None or len(df) < 11:
            self._set_eval(skip_reason="INSUFFICIENT_BARS", df_len=len(df) if df is not None else 0)
            return None

        high_col = "High" if "High" in df.columns else "high"
        # [GSD Upgrade] 從 20 改為 10
        high_10_prev = float(df[high_col].rolling(10).max().shift(1).iloc[-1])

        if close <= high_10_prev:
            self._set_eval(skip_reason="NO_STRUCTURAL_BREAKOUT", close=close,
                           high_10_prev=high_10_prev)
            return None

        # ── Behavior: volume spike + VWAP ──
        if volume_spike < 1.5:
            self._set_eval(skip_reason="VOLUME_TOO_LOW", volume_spike=volume_spike)
            return None
        if vwap > 0 and close <= vwap:
            self._set_eval(skip_reason="VWAP_REJECT", close=close, vwap=vwap)
            return None

        # ── Regime-aware threshold ──
        threshold = self._get_threshold(regime)
        if threshold is None:
            # If TRANSITION is mapped to TREND in router, it should pass here
            self._set_eval(skip_reason=f"REGIME_BLOCKED:{regime}")
            return None

        # ── Strength ──
        bs = self._compute_breakout_strength(close, high_10_prev, atr)

        # ── Confirmed breakout (full size) ──
        if bs >= threshold:
            self._signaled = True
            
            stop_p = close - (atr * 1.5)
            target_p = close + (atr * 3.0)

            self._set_eval(
                triggered=True,
                action="BUY",
                edge_score=0.85,
                bs_atr=bs,
                regime=regime,
            )

            return Signal(
                action="BUY",
                reason="ORB_V2_BREAKOUT",
                stop_loss=stop_p,
                target=target_p,
                confidence=0.85,
                quantity=1
            )

        self._set_eval(skip_reason="STRENGTH_INSUFFICIENT", bs=bs, threshold=threshold)
        return None

    def cleanup(self) -> None:
        self._reset_state()
