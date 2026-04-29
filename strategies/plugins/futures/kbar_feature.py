"""kbar_feature — Kbar Feature Strategy: regime-aligned trend continuation.

Entry logic:
- SHORT: bear regime + bearish alignment + ADX >= threshold + price <= VWAP
          + score <= threshold + momentum (MACD < 0, mom_velo < 0)
          + optional: close < recent_low breakout
- LONG:  bull regime + bullish alignment + ADX >= threshold + price >= VWAP
          + score >= threshold + momentum (MACD > 0, macd_rising, mom_velo > 0)
          + optional: close > recent_high breakout

Exit logic:
- Stop loss / take profit hit
- VWAP + MACD reversal
- Momentum reversal
- Max hold bars reached

Stop loss = close +/- stop_atr_mult * ATR (absolute price, NOT points)
"""
from __future__ import annotations

import math
from typing import Any

from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.strategy_eval import StrategyEval
from core.signal import Signal

# Columns required in the feature-enriched bar dict
_REQUIRED_COLUMNS = {
    "Close", "High", "Low", "atr", "vwap", "adx", "score",
    "regime", "bear_align", "bull_align", "bearish_align", "bullish_align",
    "macd_hist", "macd_rising", "mom_velo", "recent_high", "recent_low",
    "price_vs_vwap", "volume_spike",
}


class KbarFeature(StrategyBase):
    """Regime-aligned trend continuation with multi-timeframe feature confirmation.

    Optimized for short-side trading on TMF/TXF during bear/weak regimes.
    Uses ATR-based position sizing, momentum triggers, and breakout confirmation.
    """

    # ── Required Properties ──────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "kbar_feature"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "backtest_pf": 0.0,
            "backtest_wr": 0.0,
            "backtest_maxdd": 0.0,
            "market_regime": "trending",
            "description": "Kbar Feature: regime-aligned trend continuation with MTF feature confirmation",
            "indicators": ["atr", "vwap", "adx", "macd", "mom_velo", "regime", "align"],
        }

    # ── Required Lifecycle ───────────────────────────────────────────────

    def init(self, context: StrategyContext) -> None:
        """Called once when strategy is activated."""
        self._bars_held = 0       # bars since entry (tracked per signal)

    def on_bar(self, context: StrategyContext) -> Signal | None:
        """Called on every new bar. Returns Signal or None."""
        bar = context.market.last_bar
        params = context.config.get("params", {})

        # ── Read config params ──────────────────────────────────────────
        adx_threshold = params.get("adx_threshold", 20.0)
        stop_atr_mult = params.get("stop_atr_mult", 1.2)
        take_profit_atr_mult = params.get("take_profit_atr_mult", 2.0)
        max_hold_bars = params.get("max_hold_bars", 12)
        long_enabled = params.get("long_enabled", False)
        short_enabled = params.get("short_enabled", True)
        require_breakout = params.get("require_breakout", True)
        vwap_buffer = params.get("vwap_buffer", 0.0)
        score_short_threshold = params.get("score_short_threshold", -20.0)
        score_long_threshold = params.get("score_long_threshold", 20.0)

        # ── Read bar data ───────────────────────────────────────────────
        close = float(bar.get("Close", 0.0))
        high = float(bar.get("High", 0.0))
        low = float(bar.get("Low", 0.0))
        atr = float(bar.get("atr", 0.0))
        vwap = float(bar.get("vwap", close))
        adx = float(bar.get("adx", 0.0) or 0.0)
        score = float(bar.get("score", 0.0) or 0.0)
        regime = str(bar.get("regime", "") or "")
        bear_align = bool(bar.get("bear_align", False))
        bull_align = bool(bar.get("bull_align", False))
        bearish_align = bool(bar.get("bearish_align", False))
        bullish_align = bool(bar.get("bullish_align", False))
        macd_hist = float(bar.get("macd_hist", 0.0) or 0.0)
        macd_rising = bool(bar.get("macd_rising", False))
        mom_velo = float(bar.get("mom_velo", 0.0) or 0.0)
        recent_high = float(bar.get("recent_high", close))
        recent_low = float(bar.get("recent_low", close))
        volume_spike = bool(bar.get("volume_spike", False))

        # ── Read position ───────────────────────────────────────────────
        pos_size = context.position.size  # +N long, -N short, 0 flat
        pos_side = "LONG" if pos_size > 0 else ("SHORT" if pos_size < 0 else "FLAT")
        entry_price = context.position.entry_price
        current_sl = context.position.current_stop_loss

        # ── Validate required columns (debug check) ─────────────────────
        missing = _REQUIRED_COLUMNS - set(bar.keys())
        if missing:
            return Signal(
                action="HOLD",
                reason=f"MISSING_COLUMNS:{sorted(missing)}",
                stop_loss=0.0,
                confidence=0.0,
            )

        if atr <= 0 or math.isnan(atr):
            return None

        # ── ENTRY: flat position ────────────────────────────────────────
        if pos_side == "FLAT":
            return self._evaluate_entry(
                short_enabled=short_enabled,
                long_enabled=long_enabled,
                regime=regime,
                bear_align=bear_align,
                bull_align=bull_align,
                bearish_align=bearish_align,
                bullish_align=bullish_align,
                adx=adx,
                adx_threshold=adx_threshold,
                close=close,
                vwap=vwap,
                vwap_buffer=vwap_buffer,
                score=score,
                score_short_threshold=score_short_threshold,
                score_long_threshold=score_long_threshold,
                macd_hist=macd_hist,
                macd_rising=macd_rising,
                mom_velo=mom_velo,
                recent_high=recent_high,
                recent_low=recent_low,
                require_breakout=require_breakout,
                atr=atr,
                stop_atr_mult=stop_atr_mult,
                take_profit_atr_mult=take_profit_atr_mult,
                volume_spike=volume_spike,
                adx_raw=adx,
                score_raw=score,
            )

        # ── EXIT: has position ──────────────────────────────────────────
        self._bars_held += 1
        return self._evaluate_exit(
            pos_side=pos_side,
            close=close,
            high=high,
            low=low,
            vwap=vwap,
            macd_hist=macd_hist,
            mom_velo=mom_velo,
            current_sl=current_sl,
            entry_price=entry_price,
            stop_atr_mult=stop_atr_mult,
            take_profit_atr_mult=take_profit_atr_mult,
            atr=atr,
            max_hold_bars=max_hold_bars,
        )

    # ── Optional hooks ──────────────────────────────────────────────────

    def cleanup(self) -> None:
        self._bars_held = 0

    # ── Private helpers ─────────────────────────────────────────────────

    def _size_mult(self, adx: float, score: float, volume_spike: bool) -> float:
        """Calculate position size multiplier based on signal strength."""
        mult = 1.0
        if adx >= 25:
            mult += 0.25
        if adx >= 30:
            mult += 0.25
        abs_score = abs(score)
        if abs_score >= 80:
            mult += 0.25
        elif abs_score >= 50:
            mult += 0.10
        if volume_spike:
            mult += 0.10
        return min(mult, 1.75)

    def _evaluate_entry(
        self,
        short_enabled: bool,
        long_enabled: bool,
        regime: str,
        bear_align: bool,
        bull_align: bool,
        bearish_align: bool,
        bullish_align: bool,
        adx: float,
        adx_threshold: float,
        close: float,
        vwap: float,
        vwap_buffer: float,
        score: float,
        score_short_threshold: float,
        score_long_threshold: float,
        macd_hist: float,
        macd_rising: bool,
        mom_velo: float,
        recent_high: float,
        recent_low: float,
        require_breakout: bool,
        atr: float,
        stop_atr_mult: float,
        take_profit_atr_mult: float,
        volume_spike: bool,
        adx_raw: float,
        score_raw: float,
    ) -> Signal | None:
        """Evaluate entry conditions. Returns Signal(BUY/SELL) or None."""
        # ── SHORT entry ─────────────────────────────────────────────────
        if short_enabled and regime.upper() in {"WEAK", "BEAR", "DOWN"}:
            if not (bear_align and bearish_align):
                return None
            if adx < adx_threshold:
                return None
            if close > vwap + vwap_buffer:
                return None
            if score > score_short_threshold:
                return None

            # Momentum trigger
            momentum_ok = macd_hist < 0 and mom_velo < 0
            if not momentum_ok:
                return None

            # Breakout confirmation
            if require_breakout and close >= recent_low:
                return None

            # Entry signal
            mult = self._size_mult(adx_raw, score_raw, volume_spike)
            stop = close + stop_atr_mult * atr
            tp = close - take_profit_atr_mult * atr
            self._bars_held = 0
            return Signal(
                action="SELL",
                reason="KBAR_FEATURE_SHORT",
                stop_loss=stop,
                target=tp,
                confidence=min(0.6 + mult * 0.15, 0.95),
                trail_points=atr * 1.5,
                break_even_trigger=atr * 1.0,
            )

        # ── LONG entry ──────────────────────────────────────────────────
        if long_enabled and regime.upper() in {"STRONG", "BULL", "UP"}:
            if not (bull_align and bullish_align):
                return None
            if adx < adx_threshold:
                return None
            if close < vwap - vwap_buffer:
                return None
            if score < score_long_threshold:
                return None

            # Momentum trigger
            momentum_ok = macd_hist > 0 and macd_rising and mom_velo > 0
            if not momentum_ok:
                return None

            # Breakout confirmation
            if require_breakout and close <= recent_high:
                return None

            # Entry signal
            mult = self._size_mult(adx_raw, score_raw, volume_spike)
            stop = close - stop_atr_mult * atr
            tp = close + take_profit_atr_mult * atr
            self._bars_held = 0
            return Signal(
                action="BUY",
                reason="KBAR_FEATURE_LONG",
                stop_loss=stop,
                target=tp,
                confidence=min(0.6 + mult * 0.15, 0.95),
                trail_points=atr * 1.5,
                break_even_trigger=atr * 1.0,
            )

        return None

    def _evaluate_exit(
        self,
        pos_side: str,
        close: float,
        high: float,
        low: float,
        vwap: float,
        macd_hist: float,
        mom_velo: float,
        current_sl: float | None,
        entry_price: float,
        stop_atr_mult: float,
        take_profit_atr_mult: float,
        atr: float,
        max_hold_bars: int,
    ) -> Signal | None:
        """Evaluate exit conditions. Returns Signal(EXIT) or None."""
        if pos_side == "LONG":
            # Stop loss hit
            if current_sl is not None and low <= current_sl:
                return Signal("EXIT", "KBAR_STOP_LONG", current_sl, confidence=1.0)
            # Take profit hit (target = entry + take_profit_atr_mult * atr)
            if entry_price > 0 and atr > 0:
                tp_price = entry_price + take_profit_atr_mult * atr
                if high >= tp_price:
                    return Signal("EXIT", "KBAR_TP_LONG", tp_price, confidence=1.0)
            # VWAP + MACD reversal
            if close < vwap and macd_hist < 0:
                return Signal("EXIT", "KBAR_LOST_VWAP_LONG", close, confidence=0.8)
            # Momentum reversal
            if mom_velo < 0:
                return Signal("EXIT", "KBAR_MOM_REV_LONG", close, confidence=0.7)
            # Max hold
            if self._bars_held >= max_hold_bars:
                return Signal("EXIT", "KBAR_MAX_HOLD_LONG", close, confidence=0.6)

        elif pos_side == "SHORT":
            # Stop loss hit
            if current_sl is not None and high >= current_sl:
                return Signal("EXIT", "KBAR_STOP_SHORT", current_sl, confidence=1.0)
            # Take profit hit
            if entry_price > 0 and atr > 0:
                tp_price = entry_price - take_profit_atr_mult * atr
                if low <= tp_price:
                    return Signal("EXIT", "KBAR_TP_SHORT", tp_price, confidence=1.0)
            # VWAP + MACD reversal
            if close > vwap and macd_hist > 0:
                return Signal("EXIT", "KBAR_RECLAIMED_VWAP_SHORT", close, confidence=0.8)
            # Momentum reversal
            if mom_velo > 0:
                return Signal("EXIT", "KBAR_MOM_REV_SHORT", close, confidence=0.7)
            # Max hold
            if self._bars_held >= max_hold_bars:
                return Signal("EXIT", "KBAR_MAX_HOLD_SHORT", close, confidence=0.6)

        return None
