"""
trend_continuation_v1 — Trend Continuation (补票) Strategy.

Optimized for entering strong trends during high-level consolidation or minor pullbacks.
Strictly prohibited in WEAK/CHOP/RISK_OFF regimes.

Core logic:
- LONG ONLY (for now)
- score >= 70
- mom_state == 3 (Strong Bullish Momentum)
- close > vwap
- close > ema_fast > ema_slow (Alignment)
- breakout_strength < 0.15 (Not a fresh breakout, avoiding chase)
- volume_spike >= 1.2
- regime in {"STRONG", "TREND", "RISK_ON", "TRANSITION"}
- distance_from_vwap_atr < 1.2 (Overextended guard)
- recent_pullback_pct >= 0.001 (Must have some cooling off)
"""
from __future__ import annotations

import logging
from typing import Any
from datetime import datetime

import pandas as pd
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.signal import Signal

logger = logging.getLogger(__name__)

class TrendContinuationV1(StrategyBase):
    """v1: Trend continuation 'catch-up' entry for strong bull markets."""

    @property
    def name(self) -> str:
        return "trend_continuation_v1"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.1",
            "market_regime": "strong_trend",
            "description": "Trend continuation entry for strong bull markets (Gap/Pullback recovery)",
            "indicators": ["score", "mom_state", "vwap", "ema_fast", "ema_slow", "breakout_strength", "volume_spike", "atr"],
        }

    def init(self, context: StrategyContext) -> None:
        params = context.config.get("params", {})
        self.stop_atr_mult = params.get("stop_atr_mult", 1.2)
        self.take_profit_atr_mult = params.get("take_profit_atr_mult", 1.8)
        self.min_score = params.get("min_score", 70)
        self.max_breakout = params.get("max_breakout", 0.15)
        self.min_vol_spike = params.get("min_vol_spike", 1.2)
        self.max_vwap_dist_atr = params.get("max_vwap_dist_atr", 1.2)
        self.min_pullback_pct = params.get("min_pullback_pct", 0.001)
        self.max_hold_minutes = params.get("max_hold_minutes", 30)
        self.shadow_mode = params.get("shadow_mode", True)
        
        # State tracking (calibrations 2 & 3 + Quantitative Layer)
        self._entry_ts: datetime | None = None
        self._entry_vwap: float | None = None
        self._entry_price: float | None = None
        self._virtual_pos: dict[str, Any] | None = None  # For shadow PnL tracking

    def on_bar(self, context: StrategyContext) -> Signal | None:
        bar = context.market.last_bar
        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        # ── 1. Position & Time Stop Guard ──
        pos_size = context.position.size
        
        # Safe timestamp handling
        ts_raw = bar.get("timestamp")
        if isinstance(ts_raw, datetime):
            now = ts_raw
        elif isinstance(ts_raw, str) and ts_raw:
            now = datetime.fromisoformat(ts_raw)
        elif isinstance(ts_raw, pd.Timestamp):
            now = ts_raw.to_pydatetime()
        else:
            now = datetime.now()

        close = float(bar.get("Close", 0))
        high = float(bar.get("High", 0))
        low = float(bar.get("Low", 0))
        atr = float(bar.get("atr", 50))

        # A. Handle Real Position Exit
        if pos_size != 0:
            if self._entry_ts is None:
                self._entry_ts = now
                self._entry_vwap = float(bar.get("vwap", 0))
                self._entry_price = context.position.entry_price

            elapsed_minutes = (now - self._entry_ts).total_seconds() / 60
            unrealized_pnl = context.position.unrealized_pnl
            
            # Time Stop: 30 minutes without profit
            if elapsed_minutes >= self.max_hold_minutes and unrealized_pnl <= 0:
                logger.info(f"[TREND_CONTINUATION_EXIT] TIME_STOP. elapsed={elapsed_minutes:.1f}m pnl={unrealized_pnl}")
                self._reset_trade_state()
                self._set_eval(triggered=True, action="EXIT", reason="TIME_STOP_CONTINUATION")
                return Signal("EXIT", "TIME_STOP_CONTINUATION", confidence=1.0)
            
            self._set_eval(skip_reason="POSITION_OPEN", pnl=unrealized_pnl, elapsed=elapsed_minutes)
            return None

        # B. Handle Virtual Position Exit (Calibration 3: Shadow PnL)
        if self.shadow_mode and self._virtual_pos:
            v_entry = self._virtual_pos["entry_price"]
            v_ts = self._virtual_pos["entry_ts"]
            v_sl = self._virtual_pos["stop_loss"]
            v_tp = self._virtual_pos["target"]
            
            elapsed = (now - v_ts).total_seconds() / 60
            v_pnl = close - v_entry
            
            exit_reason = None
            exit_price = close
            
            if low <= v_sl:
                exit_reason = "SHADOW_STOP_LOSS"
                exit_price = v_sl
            elif high >= v_tp:
                exit_reason = "SHADOW_TAKE_PROFIT"
                exit_price = v_tp
            elif elapsed >= self.max_hold_minutes and v_pnl <= 0:
                exit_reason = "SHADOW_TIME_STOP"
                exit_price = close
            
            if exit_reason:
                final_pnl = exit_price - v_entry
                logger.info(f"[TC_SHADOW_RESULT] exit={exit_reason} entry={v_entry:.0f} exit_p={exit_price:.0f} pnl_pts={final_pnl:.1f} elapsed={elapsed:.1f}m")
                self._virtual_pos = None
            else:
                self._set_eval(skip_reason="VIRTUAL_POSITION_OPEN", pnl=v_pnl)
                return None

        # Reset real state if flat
        if pos_size == 0:
            self._reset_trade_state()

        # ── 2. Data Extraction for Entry ──
        vwap = float(bar.get("vwap", 0))
        score = float(bar.get("score", 0))
        mom_state = int(bar.get("mom_state", 0))
        ema_fast = float(bar.get("ema_fast", 0))
        ema_slow = float(bar.get("ema_slow", 0))
        breakout_strength = float(bar.get("breakout_strength", 0))
        volume_spike = float(bar.get("volume_spike", 1.0))
        
        # [P1] SSOT Contract
        regime = str(bar.get("router_regime") or bar.get("regime", "UNKNOWN")).upper()
        
        recent_high = float(bar.get("recent_high", close))
        
        vwap_dist_atr = (close - vwap) / atr if atr > 0 else 999
        pullback_pct = (recent_high - close) / recent_high if recent_high > 0 else 0

        # Helper for detailed skip logging
        def log_skip(reason: str):
            if score >= self.min_score or regime == "STRONG":
                logger.info(
                    f"[TREND_CONTINUATION_SKIP] reason={reason} score={score:.1f} mom={mom_state} "
                    f"breakout={breakout_strength:.2f} dist_atr={vwap_dist_atr:.2f} pb={pullback_pct:.4f} "
                    f"regime={regime} vol={volume_spike:.2f}"
                )
            self._set_eval(skip_reason=reason, score=score, regime=regime)

        # ── 3. Regime Guard (Strict) ──
        allowed_regimes = {"STRONG", "TREND", "RISK_ON", "TRANSITION", "TRENDING"}
        if regime not in allowed_regimes:
            if score >= self.min_score:
                log_skip("REGIME_BLOCKED")
            else:
                self._set_eval(skip_reason="REGIME_NOT_ALLOWED", regime=regime)
            return None

        # ── 4. Core Trend & Momentum Logic ──
        if score < self.min_score:
            log_skip("SCORE_TOO_LOW")
            return None
        
        if mom_state != 3: 
            log_skip("MOM_STATE_NOT_3")
            return None

        if not (close > vwap and close > ema_fast > ema_slow):
            log_skip("ALIGNMENT_FAILED")
            return None

        # ── 5. Continuation vs Breakout Logic ──
        if breakout_strength >= self.max_breakout:
            log_skip("TOO_STRONG_BREAKOUT")
            return None
            
        # ── 6. Safety Guards ──
        if vwap_dist_atr >= self.max_vwap_dist_atr:
            log_skip("STRETCHED_FROM_VWAP")
            return None
            
        if pullback_pct < self.min_pullback_pct:
            log_skip("NO_PULLBACK")
            return None
            
        if volume_spike < self.min_vol_spike:
            log_skip("VOL_SPIKE_INSUFFICIENT")
            return None

        # ── 7. Signal Emission ──
        self._set_eval(triggered=True, action="BUY", edge_score=score/100.0)
        
        stop_p = close - (atr * self.stop_atr_mult)
        target_p = close + (atr * self.take_profit_atr_mult)

        if self.shadow_mode:
            # Start virtual position tracking
            self._virtual_pos = {
                "entry_price": close,
                "entry_ts": now,
                "stop_loss": stop_p,
                "target": target_p
            }
            logger.info(f"[TC_SHADOW_ENTRY] score={score:.1f} entry={close:.0f} sl={stop_p:.0f} tp={target_p:.0f}")
            logger.info(f"[TC_COMPARE] shadow_signal=BUY real_signal=BUY diff=False | score={score:.1f} price={close}")
            return Signal(action="HOLD", reason="SHADOW_BUY_TRIGGERED", confidence=score/100.0)
            
        logger.info(f"[TC_COMPARE] shadow_signal=BUY real_signal=BUY diff=False | EXECUTE price={close}")
        return Signal(
            action="BUY",
            reason="TREND_CONTINUATION_SCOUT",
            stop_loss=stop_p,
            target=target_p,
            confidence=score / 100.0,
            quantity=1
        )

    def _reset_trade_state(self):
        self._entry_ts = None
        self._entry_vwap = None
        self._entry_price = None

    def cleanup(self) -> None:
        self._reset_trade_state()
        self._virtual_pos = None
