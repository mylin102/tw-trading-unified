"""Options V2 Squeeze — Multi-timeframe squeeze alignment signal generator.

Extracted from ``live_options_squeeze_monitor.fetch_live_signal()`` and
``manage_open_position()``.  This plugin produces **only** ``Signal`` objects;
contract management, order execution, and position tracking remain in the monitor.
"""
from __future__ import annotations

import numpy as np

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext


class OptionsV2Squeeze(StrategyBase):
    """V2 Squeeze: MTF alignment + squeeze-fire + EMA trend filter."""

    @property
    def name(self) -> str:
        return "v2_squeeze"

    @property
    def metadata(self) -> dict:
        return {
            "asset_class": "options",
            "version": "2.0",
            "backtest_pf": 0.0,
            "backtest_wr": 0.0,
            "backtest_maxdd": 0.0,
            "market_regime": "trending",
            "description": "Options V2 Squeeze: MTF alignment with squeeze fire detection",
        }

    # ── init ─────────────────────────────────────────────────────────

    def init(self, context: StrategyContext) -> None:
        self._last_signal_score = 0.0
        self._last_mid_trend = ""

    # ── on_bar ───────────────────────────────────────────────────────

    def on_bar(self, context: StrategyContext) -> Signal | None:
        params = context.config.get("params", {})
        df_5m = context.market.df_5m
        df_15m = context.market.df_15m
        df_1h = context.market.df_1h

        if df_5m is None or len(df_5m) < 2:
            self._set_eval(skip_reason="NO_DATA")
            return None

        # ── 1. Compute squeeze on 5m bar ─────────────────────────────
        p5 = df_5m  # pre-computed by the monitor
        row = p5.iloc[-1]
        close = float(row.get("Close", 0))
        vwap = float(row.get("vwap", 0))
        momentum = float(row.get("momentum", 0))
        fired = bool(row.get("fired", False))
        sqz_on = bool(row.get("sqz_on", False))
        bullish_align = bool(row.get("bullish_align", False))
        bearish_align = bool(row.get("bearish_align", False))

        # ── 2. Mid-trend from 15m ────────────────────────────────────
        mid_trend = self._infer_mid_trend(df_15m)
        self._last_mid_trend = mid_trend or ""

        # ── 3. MTF alignment score ───────────────────────────────────
        available_data = {"5m": df_5m, "15m": df_15m}
        if df_1h is not None and not df_1h.empty and "momentum" in df_1h.columns:
            available_data["1h"] = df_1h
        else:
            available_data["1h"] = df_15m  # proxy

        weights = {
            "1h": float(params.get("weight_1h", 0.4)),
            "15m": float(params.get("weight_15m", 0.4)),
            "5m": float(params.get("weight_5m", 0.2)),
        }
        score = self._mtf_alignment_score(available_data, weights)
        self._last_signal_score = score

        # ── 4. Resolve raw direction ─────────────────────────────────
        entry_score = float(params.get("entry_score", 60))
        require_mid_trend = params.get("require_mid_trend", True)
        side = self._resolve_entry_side(
            row, score, close, vwap, entry_score,
            mid_trend=mid_trend, require_mid_trend=require_mid_trend,
        )

        # ── 5. V2 filters: fire + alignment ──────────────────────────
        if params.get("require_fire", True):
            fire_threshold = float(params.get("fire_score_threshold", 60))
            if not fired and abs(score) < fire_threshold:
                self._set_eval(skip_reason="NO_FIRE", score=score, fire_thresh=fire_threshold)
                return None

        if side and params.get("require_align", True):
            if side == "C" and not bullish_align:
                self._set_eval(skip_reason="ALIGNMENT_NOT_BULLISH", side="CALL", score=score)
                return None
            elif side == "P" and not bearish_align:
                self._set_eval(skip_reason="ALIGNMENT_NOT_BEARISH", side="PUT", score=score)
                return None

        if side is None:
            self._set_eval(skip_reason="NO_SIDE_RESOLVED", score=score, vwap=vwap, close=close, mid_trend=mid_trend)
            return None

        # ── 6. Build Signal ──────────────────────────────────────────
        atr = float(row.get("atr", 200.0))
        sl_mult = float(params.get("atr_sl_mult", 2.0))
        sl_pts = atr * sl_mult if atr > 0 else 60.0

        if side == "C":
            action = "BUY"
            stop_loss = close - sl_pts
            confidence = min(abs(score) / 100.0, 1.0) if score > 0 else 0.5
        else:  # P
            action = "SELL"
            stop_loss = close + sl_pts
            confidence = min(abs(score) / 100.0, 1.0) if score < 0 else 0.5

        reason = self._build_reason(sqz_on, fired, mid_trend, bullish_align, bearish_align)
        sig = Signal(action, reason, stop_loss, confidence=confidence)
        valid, msg = sig.validate()
        if not valid:
            self._set_eval(skip_reason=f"INVALID_SIGNAL:{msg}", action=action, score=score)
            return None
            
        self._set_eval(triggered=True, action=action, reason=reason, score=score)
        return sig

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _infer_mid_trend(df_15m):
        if df_15m is None or df_15m.empty or "ema_filter" not in df_15m.columns:
            return None
        last = df_15m.iloc[-1]
        return "BULL" if last["Close"] > last["ema_filter"] else "BEAR"

    @staticmethod
    def _mtf_alignment_score(data_dict, weights):
        """Replicates calculate_mtf_alignment from options_engine."""
        latest_states = {}
        for timeframe, df in data_dict.items():
            if df is None or df.empty or "momentum" not in df.columns:
                continue
            last = df.iloc[-1]
            direction = 1 if last["momentum"] > 0 else -1
            mom_state = last.get("mom_state", 1)
            strength = 1.5 if mom_state in (0, 3) else 1.0
            latest_states[timeframe] = direction * strength

        total_score = 0.0
        available_weight = 0.0
        for timeframe, value in latest_states.items():
            weight = weights.get(timeframe, 0.1)
            total_score += value * weight
            available_weight += weight

        if available_weight <= 0:
            return 0.0
        return (total_score / (1.5 * available_weight)) * 100

    @staticmethod
    def _resolve_entry_side(row, score, close, vwap, score_thresh,
                             mid_trend=None, require_mid_trend=False):
        if row.get("sqz_on", True):
            return None
        if score >= score_thresh and close >= vwap:
            if require_mid_trend and mid_trend != "BULL":
                return None
            return "C"
        if score <= -score_thresh and close <= vwap:
            if require_mid_trend and mid_trend != "BEAR":
                return None
            return "P"
        return None

    @staticmethod
    def _build_reason(sqz_on, fired, mid_trend, bullish, bearish):
        parts = ["V2_SQUEEZE"]
        if fired:
            parts.append("FIRED")
        if mid_trend:
            parts.append(mid_trend)
        if bullish:
            parts.append("BULL_ALIGN")
        if bearish:
            parts.append("BEAR_ALIGN")
        return "_".join(parts)

    def cleanup(self) -> None:
        self._last_signal_score = 0.0
        self._last_mid_trend = ""
