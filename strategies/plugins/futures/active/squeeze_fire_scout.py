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
            "version": "1.1",
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
        # ── Default Eval (SDD Compliance) ──
        self._set_eval(skip_reason="INITIALIZING")

        bar = context.market.last_bar
        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        # ── [SQUEEZE_WATCH] On every bar, print squeeze-readiness snapshot ──
        _sqz_on = bool(bar.get("sqz_on", False))
        _sqz_fire = bool(bar.get("fired") or bar.get("sqz_fire", False))
        _regime = str(getattr(context.market, "regime", "?")).upper()
        if _sqz_on or _sqz_fire or _regime in ("SQUEEZE", "WEAK", "STRETCHED", "TREND", "BEAR"):
            _close = bar.get("Close", 0)
            _vwap = bar.get("vwap", 0)
            _bs_val = bar.get("breakout_strength_atr", None)
            _bs_src = "calc"
            if _bs_val is None:
                _bs_val = bar.get("breakout_strength", 0)
                _bs_src = "fallback_bs"
            if _bs_val is None or _bs_val == 0:
                _bs_src = "default_zero"
                _bs_val = 0
            _bs_val = float(_bs_val)
            _mom = bar.get("mom_state", 0)
            _bias_raw = bar.get("router_bias", bar.get("bias", "?"))
            # Normalize bias: BULLISH/BEARISH → LONG/SHORT
            _bias_map = {"BULLISH": "LONG", "BEARISH": "SHORT", "BULL": "LONG", "BEAR": "SHORT"}
            _bias = str(_bias_map.get(str(_bias_raw).upper(), str(_bias_raw).upper()))
            _vs = bar.get("volume_spike", 0)
            _pos = context.position.size
            # Why-no-trade: mirror the first check that would fire
            if _pos != 0:
                _why = f"POSITION_OPEN:{_pos}"
            elif not _sqz_fire:
                _why = "NO_SQUEEZE_FIRE"
            elif _bias not in ("LONG", "SHORT"):
                _why = f"BIAS:{_bias}"
            elif int(_mom) < 2:
                _why = f"MOM:{_mom}"
            elif float(_bs_val) >= 0.25:
                _why = f"BREAKOUT_CONFIRMED:{_bs_val:.3f}"
            elif float(_vwap) > 0 and float(_close) > 0 and ((_bias == "LONG" and float(_close) <= float(_vwap)) or (_bias == "SHORT" and float(_close) >= float(_vwap))):
                _why = "VWAP_MISALIGN"
            else:
                _why = "READY"
            if not hasattr(self, '_sqw_prev'):
                self._sqw_prev = {"ts": "", "why": ""}
            _ts = str(bar.get("timestamp", bar.get("ts", "?")))
            # Throttle: only log when timestamp changes AND (sqz/fire active OR why changed)
            _has_sqz_activity = _sqz_on or _sqz_fire
            _why_changed = self._sqw_prev.get("why") != _why
            if _ts != self._sqw_prev.get("ts") and (_has_sqz_activity or _why_changed):
                self._sqw_prev = {"ts": _ts, "why": _why}
                print(
                    f"[SQUEEZE_WATCH] ts={_ts} sqz={int(_sqz_on)} fire={int(_sqz_fire)} "
                    f"regime={_regime} bias={_bias} close={_close:.0f} vwap={_vwap:.0f} "
                    f"bs={_bs_val:.3f}({_bs_src}) mom={_mom} vol_spike={float(_vs):.2f}/{0.8:.1f} "
                    f"pos={_pos} why={_why}",
                    flush=True,
                )

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
        # [Fix] Relaxed: Squeeze Fire Scout can evaluate in non-SQUEEZE regimes
        # because 'fired' flag only exists on the bar AFTER squeeze ends (when regime changes).
        regime = str(getattr(context.market, "regime", "UNKNOWN")).upper()
        # [GSD] Also allow STRETCHED and NORMAL to capture fires into any state
        allowed_regimes = ("SQUEEZE", "WEAK", "TREND", "BEAR", "TRANSITION", "CHOP", "STRETCHED", "NORMAL")
        if regime not in allowed_regimes:
            self._set_eval(skip_reason=f"SKIP:REGIME_NOT_ALLOWED:{regime}")
            return None

        # ═══ Squeeze fire check ═══
        # Indicator engine outputs 'fired'; schema also defines 'sqz_fire'
        sqz_fire = bar.get("fired") or bar.get("sqz_fire", False)
        if not sqz_fire:
            self._set_eval(skip_reason="NO_SQUEEZE_FIRE")
            return None

        # ═══ Bias check ═══
        # [P1] Single Source of Truth: Use unified bias from router
        bias = bar.get("router_bias") or bar.get("bias") or "NEUTRAL"
        bias = str(bias).upper().strip()
        
        if bias not in ("LONG", "SHORT"):
            self._set_eval(skip_reason=f"NO_USABLE_BIAS:{bias}", router_bias=bias)
            return None

        # [TEMP RELAX 2026-05-08] Momentum check — lowered from 3 to 2 for night session
        # Revert to >= 3 when day session resumes
        mom_state = int(bar.get("mom_state", 0))
        if mom_state < 2:
            self._set_eval(skip_reason=f"MOMENTUM_TOO_LOW mom_state={mom_state} < 2 (relaxed from 3)")
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
        else:
            self._set_eval(skip_reason="NO_VWAP_DATA")
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

    def cleanup(self) -> None:
        self._reset_state()
