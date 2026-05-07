"""Squeeze Fire Scout — early scout entry when squeeze releases but before structural breakout.

Purpose:
  Cover the gap between sqz_fire=True (squeeze release) and a confirmed structural
  breakout (breakout_strength_atr >= 0.25). In this window, momentum has fired but
  price hasn't yet broken High20 — regular strategies (adaptive_orb, trend_continuation)
  reject entry as NO_BREAKOUT or NO_STRUCTURE.

Entry conditions (all required):
  - regime == SQUEEZE
  - sqz_fire == True
  - bias == LONG or SHORT
  - mom_state >= 3
  - VWAP direction aligned with bias (if enabled)
  - breakout_strength_atr < 0.25 (still in early phase, not yet confirmed)

Risk management:
  - size = 0.25x of normal position
  - stop = 0.6 ATR (tight)
  - time_stop = 6 bars (no prolonged holding without structure)
  - No scaling: must wait for breakout_strength_atr >= 0.25 for additional size
"""

from __future__ import annotations

import logging
from typing import Any

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext
from core.strategy_eval import StrategyEval

logger = logging.getLogger(__name__)


class SqueezeFireScout(StrategyBase):
    """Scout entry for squeeze release phase, before structural breakout."""

    @property
    def name(self) -> str:
        return "squeeze_fire_scout"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "market_regime": "SQUEEZE",
            "description": "Scout entry during squeeze release, 0.25x size, tight stop, time-stop 6 bars",
            "indicators": ["sqz_fire", "mom_state", "vwap", "breakout_strength_atr"],
        }

    def init(self, context: StrategyContext) -> None:
        self._last_session = None
        self._signaled = False
        self._bar_count = 0

    def _reset_state(self):
        self._signaled = False
        self._bar_count = 0

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

        # ═══ Regime guard ═══
        regime = str(getattr(context.market, "regime", "UNKNOWN")).upper()
        if regime != "SQUEEZE":
            self._set_eval(skip_reason=f"REGIME_NOT_SQUEEZE:{regime}")
            return None

        # ═══ Squeeze fire check ═══
        sqz_fire = bar.get("sqz_fire", False)
        if not sqz_fire:
            self._set_eval(skip_reason="NO_SQUEEZE_FIRE")
            return None

        # ═══ Bias check ═══
        # Determine directional bias from bar data: mom_state + close vs vwap + breakout direction
        close = float(bar.get("Close", 0))
        vwap = float(bar.get("vwap", 0))
        bias = ""
        if close > vwap and vwap > 0:
            bias = "LONG"
        elif close < vwap and vwap > 0:
            bias = "SHORT"
        # If no VWAP, fall back to breakout_strength direction
        if not bias:
            bear_bs = float(bar.get("bear_breakout_strength", 0))
            bull_bs = float(bar.get("breakout_strength", 0))
            if bull_bs > bear_bs:
                bias = "LONG"
            elif bear_bs > bull_bs:
                bias = "SHORT"
        if bias not in ("LONG", "SHORT"):
            self._set_eval(skip_reason=f"NO_USABLE_BIAS:{bias}")
            return None

        # ═══ Momentum check ═══
        mom_state = int(bar.get("mom_state", 0))
        if mom_state < 3:
            self._set_eval(skip_reason=f"MOMENTUM_TOO_LOW mom_state={mom_state} < 3")
            return None

        # ═══ Breakout strength guard: only scout when < 0.25 (pre-confirmation) ═══
        bs = float(bar.get("breakout_strength_atr", bar.get("breakout_strength", 0)))
        if bs >= 0.25:
            self._set_eval(skip_reason=f"BREAKOUT_CONFIRMED bs={bs:.3f} >= 0.25 — scout not needed")
            return None

        # ═══ VWAP direction check ═══
        vwap = float(bar.get("vwap", 0))
        close = float(bar.get("Close", 0))
        if vwap > 0:
            if bias == "LONG" and close <= vwap:
                self._set_eval(skip_reason="VWAP_BELOW", close=close, vwap=vwap)
                return None
            if bias == "SHORT" and close >= vwap:
                self._set_eval(skip_reason="VWAP_ABOVE", close=close, vwap=vwap)
                return None

        # ═══ All conditions met — fire scout ═══
        self._signaled = True

        atr = float(bar.get("atr", 50))
        action = "BUY" if bias == "LONG" else "SELL"
        direction = 1 if bias == "LONG" else -1

        # Tight stop: 0.6 ATR
        stop_price = close - direction * (atr * 0.6)

        # Time stop managed externally (monitor handles time_stop_bars)

        signal = Signal(
            action=action,
            reason="SQUEEZE_FIRE_SCOUT",
            stop_loss=stop_price,
            confidence=0.6,  # scout — lower confidence than full entry
            quantity=1,      # base lot; size_multiplier=0.25 applied by monitor
        )
        # Attach metadata for the monitor's size adjustment
        signal.metadata = {
            "size_multiplier": 0.25,
            "scout": True,
            "breakout_strength": bs,
            "mom_state": mom_state,
            "bias": bias,
            "stop_atr_mult": 0.6,
            "time_stop_bars": 6,
            "reason": "SQUEEZE_FIRE_SCOUT",
        }

        self._set_eval(
            triggered=True,
            action=action,
            edge_score=0.6,
            entry_type="SQUEEZE_FIRE_SCOUT",
            breakout_strength=bs,
            mom_state=mom_state,
            bias=bias,
            note=f"scout 0.25x size | bs={bs:.3f} stop={atr*0.6:.1f}pts",
        )

        return signal
