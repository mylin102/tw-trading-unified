"""adaptive_orb_v2 — Breakout Engine v2: Scout/Scale entry with ATR-normalized breakout.

Key differences vs v1:
  - ATR-normalized breakout_strength (was: ORB range)
  - Dual threshold: 0.15 early (scout 0.3 size) / 0.25 confirmed (full 1.0 size)
  - Regime-aware: TREND→0.15, SQUEEZE→0.25, WEAK→0.20, CHOP→skip
  - ATR floor (dynamic: close * 0.0015)
  - Three-stage: Structure → Strength → Behavior (volume+VWAP)
  - Session stabilization: min 5 bars since open

⚠️  EXPERIMENTAL — see docs/adaptive_orb_v2_verdict.md for comparison vs v1.

Key findings from backtest:
  - CONFIRMED_BREAKOUT is profitable (PF>1, avg +$168/trade)
  - EARLY_BREAKOUT is a net loser (−$35K total)
  - Exit is too tight (1.5x ATR SL / 3x ATR TP) — MFE/MAE=1.02 vs v1's 1.74
  - Use v1 (adaptive_orb) as base for live; keep v2 for confirmed-only experiments
"""
from __future__ import annotations

import logging
from typing import Any

import pandas as pd

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.strategy_eval import StrategyEval

logger = logging.getLogger(__name__)

# ── Constants ──
ATR_FLOOR_PCT = 0.0015
MIN_BARS_AFTER_OPEN = 5
EARLY_THRESHOLD = 0.15
CONFIRMED_THRESHOLD = 0.25

# ═══ KEY CONFIG ═══
# Backtest verdict: EARLY_BREAKOUT is net negative (−$35K over 3K trades).
# CONFIRMED_BREAKOUT is profitable (PF>1, avg +$168/trade).
# Exit is too tight at 1.5x/3x — MFE/MAE ratio collapses to 1.02 vs v1's 1.74.
ENABLE_EARLY_BREAKOUT = False

EXIT_CONFIG = {
    "stop_loss_atr": 2.0,
    "take_profit_atr": 4.0,
    "min_hold_bars": 20,
    "time_stop_bars": 60,
}

# Regime-aware thresholds (from breakout_engine_v2.md)
REGIME_THRESHOLDS = {
    "SQUEEZE": 0.25,
    "TREND": 0.15,
    "WEAK": 0.20,
    "CHOP": None,
}

# Normalise: BEAR → TREND, STRETCHED → WEAK, TRENDING → TREND
_REGIME_MAP = {
    "BEAR": "TREND",
    "STRETCHED": "WEAK",
    "TRENDING": "TREND",
    "CHOPPY": "CHOP",
}


class AdaptiveORBv2(StrategyBase):
    """Breakout Engine v2 — Scout/Scale Entry with regime-aware ATR breakout."""

    @property
    def name(self) -> str:
        return "adaptive_orb_v2"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "2.0",
            "market_regime": "trend_squeeze_weak",
            "description": "BEv2 Scout/Scale: ATR-normalized breakout with dual threshold entry",
            "indicators": ["atr", "vwap", "volume_spike"],
        }

    def init(self, context: StrategyContext) -> None:
        self._signaled = False
        self._last_session = None
        self._bar_count = 0

    def _get_threshold(self, regime: str) -> float | None:
        """Return threshold for the given regime, or None if regime is blocked."""
        normalized = regime.upper()
        normalized = _REGIME_MAP.get(normalized, normalized)
        return REGIME_THRESHOLDS.get(normalized)

    def _compute_breakout_strength(self, close: float, high_20_prev: float, atr: float) -> float:
        """ATR-normalized breakout strength with dynamic floor."""
        atr_floor = max(atr, close * ATR_FLOOR_PCT)
        if atr_floor <= 0:
            return 0.0
        return (close - high_20_prev) / atr_floor

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar

        # ── df_5m guard ──
        df = context.market.df_5m
        if df is None or df.empty:
            self._set_eval(skip_reason="NO_DATA", df_ready=False)
            return None

        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        # ── Session reset ──
        session_id = bar.get("trading_day", bar.get("session", 1))
        if self._last_session != session_id:
            self._signaled = False
            self._bar_count = 0
            self._last_session = session_id

        self._bar_count += 1

        if self._signaled:
            self._set_eval(skip_reason="ALREADY_SIGNALED")
            return None

        if context.position.size != 0:
            self._set_eval(skip_reason="POSITION_OPEN", position=context.position.size)
            return None

        # ── Session stabilization ──
        if self._bar_count < MIN_BARS_AFTER_OPEN:
            self._set_eval(skip_reason="SESSION_STABILIZING", bar_count=self._bar_count,
                           min_bars=MIN_BARS_AFTER_OPEN)
            return None

        close = float(bar.get("Close", 0))
        atr = float(bar.get("atr", 50))
        vwap = float(bar.get("vwap", 0))
        volume_spike = float(bar.get("volume_spike", 0))
        regime = str(getattr(context.market, "regime", "UNKNOWN"))

        # ── Structure: close > High20.shift(1) ──
        # We compute high_20_prev from df_5m
        if df is None or len(df) < 21:
            self._set_eval(skip_reason="INSUFFICIENT_BARS", df_len=len(df) if df is not None else 0)
            return None

        high_col = "High" if "High" in df.columns else "high"
        high_20_prev = float(df[high_col].rolling(20).max().shift(1).iloc[-1])

        if close <= high_20_prev:
            self._set_eval(skip_reason="NO_STRUCTURAL_BREAKOUT", close=close,
                           high_20_prev=high_20_prev)
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
            self._set_eval(skip_reason=f"REGIME_BLOCKED:{regime}")
            return None

        # ── Strength ──
        bs = self._compute_breakout_strength(close, high_20_prev, atr)

        # ── Confirmed breakout (full size) ──
        if bs >= CONFIRMED_THRESHOLD:
            self._signaled = True
            # Exit: v1-style looser bounds to preserve MFE/MAE ratio
            sl = close - EXIT_CONFIG["stop_loss_atr"] * atr
            tp = close + EXIT_CONFIG["take_profit_atr"] * atr
            self._set_eval(triggered=True, action="BUY", edge_score=bs,
                           entry_type="CONFIRMED_BREAKOUT", strength=bs,
                           regime=regime, threshold=threshold)
            return Signal("BUY", "BEV2_CONFIRMED", stop_loss=sl, target=tp,
                          confidence=0.85, quantity=1)

        # ── Early breakout (scout) — DISABLED by default ═══
        # Backtest: EARLY_BREAKOUT is net negative (−$35K / 3K trades over 3yr).
        # Scout entry adds too many low-quality trades that fees eat alive.
        # If re-enabling, use looser exit (EXIT_CONFIG) and re-validate.
        if ENABLE_EARLY_BREAKOUT and bs >= EARLY_THRESHOLD:
            self._signaled = True
            sl = close - EXIT_CONFIG["stop_loss_atr"] * atr
            tp = close + EXIT_CONFIG["take_profit_atr"] * atr
            self._set_eval(triggered=True, action="BUY", edge_score=bs,
                           entry_type="EARLY_BREAKOUT", strength=bs,
                           regime=regime, threshold=threshold,
                           note="EARLY_BREAKOUT is DISABLED by default")
            return Signal("BUY", "BEV2_EARLY", stop_loss=sl, target=tp,
                          confidence=0.75, quantity=1)

        # ── Strength below threshold ──
        self._set_eval(skip_reason="BREAKOUT_BELOW_THRESHOLD",
                       breakout_strength=bs, threshold=threshold, regime=regime)
        return None

    def cleanup(self) -> None:
        self._signaled = False
        self._bar_count = 0
