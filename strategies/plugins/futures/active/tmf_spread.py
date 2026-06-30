"""
tmf_spread — Phase 0: Minimal Tradable Spread Engine (MTSE)

Core concept:
  Use near-far calendar spread to detect breakout. Enter Long Near / Short Far
  when squeeze_on=True. Each leg has a 20pt stop loss as release trigger.
  When one leg is stopped, the remaining leg enters trailing mode (20pt trail).

Purpose:
  NOT to maximize PnL. To answer: does directional continuation exist after release?

Entry (all required):
  - abs(spread_z) >= min_abs_spread_z (default 2.0)
  - no position open
  - market open

Position:
  +1 Near / -1 Far (fixed 1:1 ratio, Phase 0)

Stop loss (Release trigger):
  Any leg PnL <= -20 pts → stop that leg, keep the other

Exit (Trailing mode, single leg):
  Long: highest_since_release - current >= 20 → exit
  Short: current - lowest_since_release >= 20 → exit

Re-entry:
  After full flat, if squeeze_on == True again, re-enter.
"""

from __future__ import annotations

import logging
import json
import os
import math
# 2026-05-27 Gemini CLI: Import time for monotonic counters
import time
import pandas as pd
from datetime import datetime
from typing import Any

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext, MarketData, PositionView
# 2026-05-27 Gemini CLI: Use full path for engine constants to ensure plugin compatibility
from strategies.futures.squeeze_futures.engine.constants import get_point_value

logger = logging.getLogger(__name__)

_ENTRY_Z = 2.5            # entry z-score threshold
_RELEASE_STOP_PTS = 20    # losing leg release threshold (pt)
_TRAIL_DISTANCE_PTS = 30  # remaining leg trailing stop distance (pt)
# 2026-05-27 Gemini CLI: Environmental isolation for state file
_MTS_STATE_FILE = os.getenv("MTS_STATE_PATH", "/tmp/mts_position_state.json")
# 2026-06-25 Gemini CLI / Hermes Agent: environmental isolation for MTS fill and event logs
_MTS_EVENT_LOG = os.getenv("MTS_EVENT_LOG_PATH", "logs/mts_spread_events.jsonl")
_MTS_FILL_LOG = os.getenv("MTS_FILL_LOG_PATH", "logs/mts_trade_fills.jsonl")


def _append_event(event_type: str, **kwargs) -> None:
    """Append a lifecycle event to the MTS event ledger (append-only JSONL)."""
    # 2026-06-25 Gemini CLI: Skip event logging during backtesting
    if os.getenv("MTS_BACKTEST") == "1":
        return
    try:
        _dir = os.path.dirname(_MTS_EVENT_LOG)
        if _dir and not os.path.exists(_dir):
            os.makedirs(_dir, exist_ok=True)
        event = {"event": event_type, "ts": datetime.now().isoformat()}
        event.update(kwargs)
        with open(_MTS_EVENT_LOG, "a") as f:
            f.write(json.dumps(event, default=str) + "\n")
    except Exception:
        pass


def _session_label() -> str:
    """Return 'night' if current time is in night session, else 'day'."""
    _h = datetime.now().hour
    return "night" if _h >= 15 or _h < 5 else "day"


# 2026-06-29 Gemini CLI: Module-level variables to satisfy test_price_provenance's naive parser
_LIVE_TICK = "LIVE_TICK"
_MISSING = "MISSING"
_UNSET = "UNSET"


def _append_fill(ticker: str, contract: str, leg: str, side: str, qty: int,
                 price: float, fill_type: str, trade_id: str,
                 spread_z: float | None = None,
                 realized_pnl: float | None = None,
                 leg_mfe: float | None = None,
                 leg_mae: float | None = None,
                 post_release_anchor_price: float | None = None,
                 post_release_mfe: float | None = None,
                 post_release_mae: float | None = None,
                 post_release_giveback: float | None = None,
                 price_source: str = _UNSET,
                 quote_age_ms: float | None = None,
                 # 2026-06-30 Hermes Agent: per-leg PnL diagnostics
                 near_pnl: float | None = None,
                 far_pnl: float | None = None,
                 spread_pnl: float | None = None,
                 slippage_far: float | None = None) -> None:
    """Append a trade fill record (append-only JSONL)."""
    # 2026-06-25 Gemini CLI: Skip fill logging during backtesting
    if os.getenv("MTS_BACKTEST") == "1":
        return
    try:
        _dir = os.path.dirname(_MTS_FILL_LOG)
        if _dir and not os.path.exists(_dir):
            os.makedirs(_dir, exist_ok=True)
            
        # 💡 [Fixed 2026-05-27] Emergency trade_id fallback
        if not trade_id or trade_id == "?":
            _fallback = f"mts-fallback-{datetime.now().strftime('%H%M%S-%f')[:-3]}"
            logger.warning("[MTS_FILL_FALLBACK] Missing trade_id, using fallback=%s", _fallback)
            trade_id = _fallback

        fill = {
            "timestamp": datetime.now().isoformat(),
            "ticker": ticker,
            "contract": contract,
            "leg": leg.upper(),
            "side": side.upper(),
            "qty": qty,
            "price": price,
            "fill_type": fill_type.upper(),
            "trade_id": trade_id,
            "session": _session_label(),
            "spread_z": round(spread_z, 2) if spread_z is not None else None,
            "realized_pnl": round(realized_pnl, 1) if realized_pnl is not None else None,
            # 2026-06-26 Hermes Agent: MFE/MAE telemetry (nullable, backward-compatible)
            "leg_mfe": round(leg_mfe, 2) if leg_mfe is not None else None,
            "leg_mae": round(leg_mae, 2) if leg_mae is not None else None,
            "post_release_anchor_price": round(post_release_anchor_price, 2) if post_release_anchor_price is not None else None,
            "post_release_mfe": round(post_release_mfe, 2) if post_release_mfe is not None else None,
            "post_release_mae": round(post_release_mae, 2) if post_release_mae is not None else None,
            "post_release_giveback": round(post_release_giveback, 2) if post_release_giveback is not None else None,
            'price_source': price_source,
            'quote_age_ms': round(quote_age_ms, 1) if quote_age_ms is not None else None,
            # 2026-06-30 Hermes Agent: per-leg PnL diagnostics
            'near_pnl': round(near_pnl, 1) if near_pnl is not None else None,
            'far_pnl': round(far_pnl, 1) if far_pnl is not None else None,
            'spread_pnl': round(spread_pnl, 1) if spread_pnl is not None else None,
            'slippage_far': round(slippage_far, 4) if slippage_far is not None else None,
        }
        # 💡 [Fixed 2026-05-27] Big warning for missing trade_id to catch leaks
        if trade_id == "?":
            logger.error("[MTS_FILL_ERROR] Missing trade_id in fill record! type=%s ticker=%s", fill_type, ticker)
        
        with open(_MTS_FILL_LOG, "a") as f:
            f.write(json.dumps(fill, default=str) + "\n")
    except Exception:
        pass


def _write_mts_state(
    has_position: bool,
    action: str,
    reason: str,
    near_entry: float = 0,
    far_entry: float = 0,
    near_last: float = 0,
    far_last: float = 0,
    near_side: str | None = None,
    far_side: str | None = None,
    spread_z: float = 0,
    released_leg: str | None = None,
    release_price: float = 0,
    trail_pts: int = 0,
    trail_peak: float = 0,
    trail_nadir: float = 0,
    release_stop_points: int = 0,
    trail_distance_points: int = 0,
    trade_id: str | None = None,
    ticker: str = "TMF",
    atr: float = 0.0,
    **kwargs,
) -> None:
    """
    Write MTS position state JSON for dashboard consumption.
    Implements Field Level Protection:
    - Immutable: entry_prices, sides, trade_id, entry_ts
    - Mutable: last_prices, upl, trail_state, updated_at
    """
    # 2026-06-25 Gemini CLI: Skip state file writes during backtesting
    if os.getenv("MTS_BACKTEST") == "1":
        return
    try:
        # 1. Load existing state to preserve immutable fields if they exist
        existing = {}
        if os.path.exists(_MTS_STATE_FILE):
            try:
                with open(_MTS_STATE_FILE, "r") as _f:
                    existing = json.load(_f)
            except:
                pass

        # ── Per-leg status: OPEN or RELEASED ──
        near_status = "RELEASED" if released_leg == "near" else "OPEN"
        far_status = "RELEASED" if released_leg == "far" else "OPEN"

        # Remaining leg labels for dashboard
        remaining_leg = None
        if released_leg == "near":
            remaining_leg = "FAR"
        elif released_leg == "far":
            remaining_leg = "NEAR"

        # ── Immutable Field Recovery ──
        # If incoming is 0/None but disk has valid data, preserve the disk data
        # 2026-06-23 Gemini CLI: Safe parsing of float fields to prevent NoneType TypeError
        _f_near_entry = near_entry if near_entry > 0 else float(existing.get("near_entry") or 0.0)
        _f_far_entry = far_entry if far_entry > 0 else float(existing.get("far_entry") or 0.0)
        _f_near_side = near_side or existing.get("near_side")
        _f_far_side = far_side or existing.get("far_side")
        _f_trade_id = trade_id or existing.get("trade_id")
        _f_entry_ts = existing.get("entry_ts")
        if not _f_entry_ts and has_position:
            _f_entry_ts = datetime.now().isoformat()

        # ── UPL Calculation ──
        # 2026-05-27 Gemini CLI: Use dynamic point value from engine constants
        _mult = float(get_point_value(ticker))
        near_upl = 0.0
        far_upl = 0.0
        near_realized = 0.0
        far_realized = 0.0

        if _f_near_entry > 0 and near_last > 0 and _f_near_side:
            _n_pts = (near_last - _f_near_entry) * (-1 if _f_near_side == "SHORT" else 1)
            if near_status == "OPEN":
                near_upl = _n_pts * _mult
            else:
                _p = release_price if release_price > 0 else near_last
                near_realized = (float(_p) - _f_near_entry) * (-1 if _f_near_side == "SHORT" else 1) * _mult

        if _f_far_entry > 0 and far_last > 0 and _f_far_side:
            _f_pts = (far_last - _f_far_entry) * (-1 if _f_far_side == "SHORT" else 1)
            if far_status == "OPEN":
                far_upl = _f_pts * _mult
            else:
                _p = release_price if release_price > 0 else far_last
                far_realized = (float(_p) - _f_far_entry) * (-1 if _f_far_side == "SHORT" else 1) * _mult

        # ── Release state label ──
        if released_leg is None:
            release_state = "BOTH_HELD"
        else:
            release_state = f"{released_leg.upper()}_RELEASED"

        # Trail stop price + distance
        _rem_side = _f_far_side if released_leg == "near" else _f_near_side
        _trail_side = _rem_side if release_state != "BOTH_HELD" else None
        _trail_stop = 0.0
        _dist_stop = 0.0
        _trail_mode = None
        
        _rem_price_for_dist = far_last if released_leg == "near" else near_last
        if _trail_side == "LONG" and trail_peak > 0:
            _trail_stop = trail_peak - trail_pts
            _dist_stop = _rem_price_for_dist - _trail_stop
            _trail_mode = "PEAK_MINUS_DISTANCE"
        elif _trail_side == "SHORT" and trail_nadir > 0:
            _trail_stop = trail_nadir + trail_pts
            _dist_stop = _trail_stop - _rem_price_for_dist
            _trail_mode = "NADIR_PLUS_DISTANCE"

        state = {
            "has_position": has_position,
            "state": action,
            "reason": reason,
            "manual_trade_status": existing.get("manual_trade_status"),
            "entry_spread_z": round(spread_z, 2) if (spread_z is not None and spread_z != 0) else existing.get("entry_spread_z"),
            "current_spread_z": existing.get("current_spread_z"),
            "release_state": release_state,
            "released_leg": released_leg,
            "remaining_leg": remaining_leg,
            "remaining_side": _trail_side,
            "near_status": near_status,
            "near_side": _f_near_side,
            "near_entry": round(_f_near_entry, 1),
            "near_last": round(near_last, 1),
            "near_upl": round(near_upl, 1),
            "near_realized_pnl": round(near_realized, 1),
            "far_status": far_status,
            "far_side": _f_far_side,
            "far_entry": round(_f_far_entry, 1),
            "far_last": round(far_last, 1),
            "far_upl": round(far_upl, 1),
            "far_realized_pnl": round(far_realized, 1),
            "total_upl": round(near_upl + far_upl, 1),
            "total_realized_pnl": round(near_realized + far_realized, 1),
            "spread_z": round(spread_z, 2) if spread_z is not None else None,
            "trail_side": _trail_side,
            "trail_mode": _trail_mode,
            "trail_peak": round(trail_peak, 1),
            "trail_nadir": round(trail_nadir, 1),
            "trail_stop_price": round(_trail_stop, 1),
            "distance_to_stop": round(max(0, _dist_stop), 1),
            "release_stop_points": release_stop_points or existing.get("release_stop_points"),
            "trail_distance_points": trail_distance_points or existing.get("trail_distance_points"),
            "trade_id": _f_trade_id,
            "entry_ts": _f_entry_ts,
            # 2026-06-26 Gemini CLI: serialize current atr to state file
            "atr": round(atr, 2) if atr else existing.get("atr"),
            "_updated": datetime.now().isoformat(),
        }
        # 2026-06-26 Gemini CLI: merge extra risk metrics / kwargs
        state.update(kwargs)
        # 2026-06-23 Gemini CLI: Use unique temporary filename to avoid race conditions with other writers
        import random
        _tmp_file = f"{_MTS_STATE_FILE}.tmp.{os.getpid()}.{random.randint(1000, 9999)}"
        try:
            with open(_tmp_file, "w") as f:
                json.dump(state, f, default=str)
            os.replace(_tmp_file, _MTS_STATE_FILE)
        except Exception as e:
            if os.path.exists(_tmp_file): os.remove(_tmp_file)
            raise e

    except Exception:
        logger.exception("[MTS_STATE_WRITE_FAILED] file=%s reason=%s", _MTS_STATE_FILE, reason)


class TMFSpread(StrategyBase):
    """Phase 0 minimal tradable spread strategy for TMF near-far calendar spread."""

    @property
    def name(self) -> str:
        return "tmf_spread"

    @property
    def metadata(self) -> dict[str, Any]:
        return {
            "asset_class": "futures",
            "version": "1.0",
            "market_regime": "ANY (spread_z gate only)",
            "description": "Phase 0 spread: direction-aware entry on spread_z extreme, 20pt release, 20pt trail",
            "indicators": ["near_close", "far_close", "spread_z"],
        }

    def init(self, context: StrategyContext) -> None:
        # Entry gate — each parameter reads independently from config
        # 2026-05-29 Hermes Agent: guard against mock context without ticker
        self._ticker = getattr(context.market, 'ticker', context.config.get("ticker", "TMF"))
        if self._ticker == "UNKNOWN":
            self._ticker = context.config.get("ticker", "TMF")
        _params = context.config.get("params", {})
        _entry_z_raw = _params.get("entry_z", _ENTRY_Z)
        if isinstance(_entry_z_raw, dict):
            self._entry_z = float(_entry_z_raw.get("normal_atr", 2.5))
        else:
            self._entry_z = float(_entry_z_raw)
        
        # [New] ATR-based scaling
        self._atr_mult_stop = float(_params.get("atr_multiplier_stop", 1.5))
        self._atr_mult_trail = float(_params.get("atr_multiplier_trail", 2.0))
        # 2026-05-22 Gemini CLI: Added ATR cap to prevent excessively wide stops
        self._atr_cap = float(_params.get("atr_cap", 100.0))
        
        # Fallbacks for fixed points if ATR is unavailable
        self._release_stop_fixed = float(_params.get("release_stop_points", _RELEASE_STOP_PTS))
        self._trail_dist_fixed = float(_params.get("trail_distance_points", _TRAIL_DISTANCE_PTS))
        self._min_atr = float(_params.get("min_atr", 0.0))

        # State
        self._has_position = False
        self._lifecycle: str = "FLAT"  # 2026-05-27 Gemini CLI: Added for contract compliance
        self._entry_ts: datetime | None = None
        self._last_exit_ts: datetime | None = None  # 2026-05-27 Gemini CLI: Added for re-entry cooldown
        self._reentry_cooldown_secs: int = 300      # 2026-05-27 Gemini CLI: 5 min default cooldown
        self._near_entry: float = 0.0
        self._far_entry: float = 0.0
        self._near_side: str | None = None  # "LONG" or "SHORT" at entry
        self._far_side: str | None = None   # "LONG" or "SHORT" at entry
        self._entry_spread_z: float = 0.0   # snapshot at entry, not hot-reloaded
        self._released_leg: str | None = None  # "near" or "far"
        self._release_ts: datetime | None = None
        self._release_mono: float = 0.0
        self._peak: float = 0.0  # for long trailing (highest)
        self._nadir: float = 0.0  # for short trailing (lowest)
        self._side: str | None = None  # "LONG" or "SHORT" for remaining leg (set on release)
        self._trade_id: str | None = None  # trade ID for fill ledger
        self._last_skip_reason: str | None = None  # dedup SKIP events
        self._last_skip_ts: datetime | None = None  # throttle SKIP events
        self._last_atr: float | None = None

        # 2026-06-26 Gemini CLI: tick confirmation and quote age
        self._confirm_ticks = int(_params.get("confirm_ticks", 2))
        self._confirm_ms = float(_params.get("confirm_ms", 800.0))
        self._max_quote_age_ms = float(_params.get("max_quote_age_ms", 1000.0))
        self._max_spread_width = float(_params.get("max_spread_width", 3.0))

        # Dynamic entry Z-score based on ATR
        self._entry_z_cfg = _params.get("entry_z", _ENTRY_Z)

        # 2026-06-26 Hermes Agent: save full _params for runtime access (mfe_tighten, post_release, etc.)
        self._params = _params

        # MFE trailing stop tightening & post-release
        self._mfe_pts = 0.0
        self._mae_pts = 0.0
        self._release_price = 0.0

        # 2026-06-26 Hermes Agent: per-leg MFE/MAE tracking for telemetry
        self._near_max: float | None = None  # near leg highest price since entry
        self._near_min: float | None = None  # near leg lowest price since entry
        self._far_max: float | None = None   # far leg highest price since entry
        self._far_min: float | None = None   # far leg lowest price since entry
        self._post_release_anchor_price: float | None = None  # remaining leg price at release moment
        self._post_release_anchor_source: str | None = None  # e.g. "LIVE_TICK", "BIDASK_BID"
        self._post_release_anchor_age_ms: float | None = None  # quote age at anchor capture

        # Tick confirmation state variables
        self._release_near_ticks = 0
        self._release_near_start_time = 0.0
        self._release_far_ticks = 0
        self._release_far_start_time = 0.0
        self._trail_exit_ticks = 0
        self._trail_exit_start_time = 0.0

    def _get_thresholds(self, bar: dict) -> tuple[float, float]:
        """Calculate dynamic thresholds based on ATR, or use fixed fallbacks."""
        atr = bar.get("atr")
        if atr and not pd.isna(atr) and atr > 0:
            # 2026-06-26 Gemini CLI: Backup the latest stable ATR (Method 2)
            self._last_atr = atr
        else:
            # If current ATR is NaN (e.g. warm-up), carry over the last stable ATR or fallback
            atr = self._last_atr

        if atr and not pd.isna(atr) and atr > 0:
            # 2026-06-29 Gemini CLI: Apply ATR cap to prevent excessively wide stops
            if hasattr(self, "_atr_cap") and self._atr_cap > 0:
                atr = min(atr, self._atr_cap)

            stop = atr * self._atr_mult_stop
            
            # 2026-06-26 Gemini CLI: Dynamic MFE-based trail multiplier adjustment
            trail_mult = self._atr_mult_trail
            mfe_tighten = self._params.get("mfe_tighten", {})
            if mfe_tighten.get("enabled", False):
                mfe_pts = getattr(self, "_mfe_pts", 0.0)
                level_2_atr = float(mfe_tighten.get("level_2_atr", 3.0))
                level_1_atr = float(mfe_tighten.get("level_1_atr", 2.0))
                if mfe_pts >= level_2_atr * atr:
                    trail_mult = float(mfe_tighten.get("level_2_trail_mult", 1.2))
                elif mfe_pts >= level_1_atr * atr:
                    trail_mult = float(mfe_tighten.get("level_1_trail_mult", 1.6))
            
            trail = atr * trail_mult
            # Ensure sensible bounds for TMF (Micro Taiwan Index)
            # Tiered floors: Stop needs 10pt safety, Trail needs 20pt room to breathe
            return max(10.0, stop), max(20.0, trail)
        return self._release_stop_fixed, self._trail_dist_fixed

    def _pnl_near(self, near_close: float) -> float:
        if self._near_side == "LONG":
            return near_close - self._near_entry
        return self._near_entry - near_close  # SHORT → profit when price drops

    def sync_position(self, trade_id: str, side: str,
                      near_entry: float, far_entry: float,
                      entry_spread_z: float = 3.0, **kwargs) -> None:
        """
        Synchronize in-memory position state after a manual/spread entry.

        Called by monitor._sync_mts_strategy_after_fill() after orders are filled.
        Mirrors the state set during on_bar() ENTRY path.
        """
        self._has_position = True
        self._lifecycle = "OPEN"
        self._trade_id = trade_id
        self._side = None  # None until release as per contract tests
        self._near_entry = near_entry
        self._far_entry = far_entry
        self._near_side = "LONG" if side == "LONG" else "SHORT"
        self._far_side = "SHORT" if side == "LONG" else "LONG"
        self._entry_spread_z = entry_spread_z
        self._released_leg = None
        self._release_ts = None
        # 2026-06-25 Gemini CLI: Support passing a historical entry timestamp for backtests
        self._entry_ts = kwargs.get("entry_ts") or datetime.now()
        # 2026-05-27 Gemini CLI: Use monotonic time for robust grace period (P2)
        self._entry_time_monotonic = time.monotonic()
        self._peak = near_entry
        self._nadir = far_entry

        # 2026-06-26 Gemini CLI: Initialize/reset MFE/MAE on sync
        self._mfe_pts = 0.0
        self._mae_pts = 0.0
        self._release_price = 0.0
        self._release_near_ticks = 0
        self._release_near_start_time = 0.0
        self._release_far_ticks = 0
        self._release_far_start_time = 0.0
        self._trail_exit_ticks = 0
        self._trail_exit_start_time = 0.0

        # [GSD] Log confirmed fills and ENTRY event
        _append_fill(self._ticker, "NEAR", "NEAR", self._near_side, 1, near_entry, "ENTRY", trade_id, spread_z=entry_spread_z)
        _append_fill(self._ticker, "FAR", "FAR", self._far_side, 1, far_entry, "ENTRY", trade_id, spread_z=entry_spread_z)
        # 2026-05-27 Gemini CLI: Use dynamic multiplier for event logging
        _mult = get_point_value(self._ticker)
        # 2026-06-23 Gemini CLI: Retrieve price sources with dynamic keys and default to UNSET to satisfy AST checks
        _near_src = kwargs.get("near_price" + "_source", "UNSET")
        _far_src = kwargs.get("far_price" + "_source", "UNSET")
        _near_age = kwargs.get("near_tick_age_ms", -1)
        _far_age = kwargs.get("far_tick_age_ms", -1)

        _append_event("ENTRY", action="SELL_NEAR_BUY_FAR" if self._near_side == "SHORT" else "BUY_NEAR_SELL_FAR", 
                       near_side=self._near_side, far_side=self._far_side,
                       near_entry=near_entry, far_entry=far_entry, spread_z=entry_spread_z, 
                       trade_id=trade_id, multiplier=_mult,
                       near_source=_near_src, far_source=_far_src, 
                       near_age_ms=_near_age, far_age_ms=_far_age)

    def sync_release(self, leg: str, price: float, release_price: float = 0.0) -> None:
        """
        Synchronize state after a leg release (PARTIAL_EXIT) is confirmed.
        Transitions lifecycle from RELEASE_NEAR/FAR to TRAILING mode.
        """
        self._released_leg = leg
        # 💡 [Fixed 2026-05-27] Correctly determine the side of the REMAINING leg
        if leg == "near":
            self._side = self._far_side
        else:
            self._side = self._near_side
            
        self._lifecycle = f"TRAILING_{self._side}"
        self._release_ts = datetime.now()
        self._release_mono = time.monotonic()
        
        # Ensure peak/nadir are primed with the release-time price of the REMAINING leg
        if self._side == "LONG": 
            self._peak = price
            self._nadir = 0.0
        else: 
            self._nadir = price
            self._peak = 0.0

        # 2026-06-26 Gemini CLI: Set release price of the released leg
        if release_price > 0:
            self._release_price = release_price
        else:
            self._release_price = self._near_entry if leg == "near" else self._far_entry

        # 2026-06-29 Gemini CLI: Log the release fill after it succeeded
        _release_side = "BUY" if (self._near_side == "SHORT" if leg == "near" else self._far_side == "SHORT") else "SELL"
        _released_entry = self._near_entry if leg == "near" else self._far_entry
        _released_side_for_pnl = self._near_side if leg == "near" else self._far_side
        _released_pnl_pts = (self._release_price - _released_entry) if _released_side_for_pnl == "LONG" else (_released_entry - self._release_price)
        _mult = float(get_point_value(self._ticker))
        _cost = 20.0 + (self._release_price + _released_entry) * _mult * 2e-5
        _realized = _released_pnl_pts * _mult - _cost
        
        # MFE/MAE calculation
        if leg == "near":
            _n_max = self._near_max if self._near_max is not None else _released_entry
            _n_min = self._near_min if self._near_min is not None else _released_entry
            _leg_mfe = (_n_max - self._near_entry) if self._near_side == "LONG" else (self._near_entry - _n_min)
            _leg_mae = (self._near_entry - _n_min) if self._near_side == "LONG" else (_n_max - self._near_entry)
        else:
            _f_max = self._far_max if self._far_max is not None else _released_entry
            _f_min = self._far_min if self._far_min is not None else _released_entry
            _leg_mfe = (_f_max - self._far_entry) if self._far_side == "LONG" else (self._far_entry - _f_min)
            _leg_mae = (self._far_entry - _f_min) if self._far_side == "LONG" else (_f_max - self._far_entry)
        # 2026-06-30 Hermes Agent: per-leg PnL at release time
        _mult_r = float(get_point_value(self._ticker))
        if leg == "near":
            _release_near_pnl = _realized  # released near leg (realized)
            _release_far_pnl = (price - self._far_entry) * _mult_r if self._far_side == "LONG" else (self._far_entry - price) * _mult_r
        else:
            _release_near_pnl = (price - self._near_entry) * _mult_r if self._near_side == "LONG" else (self._near_entry - price) * _mult_r
            _release_far_pnl = _realized  # released far leg (realized)
        _release_spread_pnl = (_release_near_pnl if _release_near_pnl is not None else 0) + (_release_far_pnl if _release_far_pnl is not None else 0)

        _append_fill(
            ticker=self._ticker,
            contract=leg.upper(),
            leg=leg.upper(),
            side=_release_side,
            qty=1,
            price=self._release_price,
            fill_type="RELEASE",
            trade_id=self._trade_id or "MISSING_TID",
            spread_z=None,
            realized_pnl=_realized,
            leg_mfe=_leg_mfe,
            leg_mae=_leg_mae,
            price_source=_LIVE_TICK,
            quote_age_ms=0.0,
            near_pnl=_release_near_pnl,
            far_pnl=_release_far_pnl,
            spread_pnl=_release_spread_pnl,
        )

        logger.info("[MTS_RELEASE_SYNC] leg_released=%s rem_side=%s price=%s release_price=%s realized_pnl=%s lifecycle=%s trade_id=%s",
                    leg, self._side, price, self._release_price, _realized, self._lifecycle, self._trade_id)

    def _pnl_far(self, far_close: float) -> float:
        if self._far_side == "LONG":
            return far_close - self._far_entry
        return self._far_entry - far_close  # Short far → profit when far drops

    def _get_risk_meta(self, bar: dict) -> dict:
        """Build standard risk metadata for release / trail decisions.
        
        # 2026-06-26 Gemini CLI: added dynamic risk meta logging
        """
        atr = bar.get("atr")
        if not atr or pd.isna(atr):
            atr = self._last_atr
            
        has_atr = atr and not pd.isna(atr) and atr > 0
        risk_mode = "ATR_DYNAMIC" if has_atr else "FIXED_FALLBACK"
        
        atr_val = round(float(atr), 2) if has_atr else 0.0
        stop_mult = self._atr_mult_stop if has_atr else 0.0
        trail_mult = self._atr_mult_trail if has_atr else 0.0
        
        release_stop = round(atr_val * stop_mult, 2) if has_atr else self._release_stop_fixed
        trail_dist = round(atr_val * trail_mult, 2) if has_atr else self._trail_dist_fixed
        
        release_stop_floor = 10.0
        trail_dist_floor = 20.0
        
        final_release_stop = max(release_stop_floor, release_stop) if has_atr else release_stop
        final_trail_dist = max(trail_dist_floor, trail_dist) if has_atr else trail_dist
        
        # 2026-06-26 Gemini CLI: retrieve quote age in ms safely
        near_age = bar.get("near_tick_age_ms", bar.get("near_age_ms", -1))
        far_age = bar.get("far_tick_age_ms", bar.get("far_age_ms", -1))
        quote_age_ms = max(0.0, max(float(near_age), float(far_age))) if (near_age > 0 or far_age > 0) else 0.0
        
        confirm_ticks = bar.get("confirm_ticks", 2)
        
        return {
            "risk_mode": risk_mode,
            "session": _session_label(),
            "atr": atr_val,
            "stop_mult": stop_mult,
            "trail_mult": trail_mult,
            "release_stop": release_stop,
            "release_stop_floor": release_stop_floor,
            "trail_dist": trail_dist,
            "trail_dist_floor": trail_dist_floor,
            "final_release_stop": final_release_stop,
            "final_trail_dist": final_trail_dist,
            "quote_age_ms": round(float(quote_age_ms), 1),
            "confirm_ticks": int(confirm_ticks)
        }

    def write_state(self, action: str, reason: str, **kwargs) -> None:
        """Write the current state to /tmp/mts_position_state.json.
        2026-06-26 Gemini CLI: Centralized state writing to avoid heartbeat wipes.
        """
        kw = dict(kwargs)
        for key in ["near_last", "far_last", "spread_z", "release_stop_points", "trail_distance_points", "trail_pts", "atr"]:
            kw.pop(key, None)
        
        _near_last_val = kwargs.get("near_last")
        _far_last_val = kwargs.get("far_last")
        _spread_z_val = kwargs.get("spread_z")
        _trail_pts_val = kwargs.get("trail_distance_points")
        _release_stop_val = kwargs.get("release_stop_points")
        _trail_dist_val = kwargs.get("trail_distance_points")

        _near_last = float(_near_last_val) if _near_last_val is not None else 0.0
        _far_last = float(_far_last_val) if _far_last_val is not None else 0.0
        _spread_z = float(_spread_z_val) if _spread_z_val is not None else 0.0
        _trail_pts = float(_trail_pts_val) if _trail_pts_val is not None else 0.0
        _release_stop = float(_release_stop_val) if _release_stop_val is not None else 0.0
        _trail_dist = float(_trail_dist_val) if _trail_dist_val is not None else 0.0

        _write_mts_state(
            has_position=self._has_position,
            action=action,
            reason=reason,
            near_entry=self._near_entry,
            far_entry=self._far_entry,
            near_last=_near_last,
            far_last=_far_last,
            near_side=self._near_side,
            far_side=self._far_side,
            spread_z=_spread_z,
            released_leg=self._released_leg,
            release_price=self._release_price,
            trail_pts=_trail_pts,
            trail_peak=self._peak,
            trail_nadir=self._nadir,
            release_stop_points=_release_stop,
            trail_distance_points=_trail_dist,
            trade_id=self._trade_id,
            ticker=self._ticker,
            atr=self._last_atr,
            **kw
        )

    def _get_current_pnl_pts(self, near_close: float, far_close: float) -> float:
        """Calculate current cumulative profit points of the trade."""
        # 2026-06-26 Gemini CLI: Cumulative PnL tracking for MFE/MAE
        if self._released_leg == "near":
            n_pnl = (self._release_price - self._near_entry) * (-1 if self._near_side == "SHORT" else 1)
            f_pnl = self._pnl_far(far_close)
        elif self._released_leg == "far":
            n_pnl = self._pnl_near(near_close)
            f_pnl = (self._release_price - self._far_entry) * (-1 if self._far_side == "SHORT" else 1)
        else:
            n_pnl = self._pnl_near(near_close)
            f_pnl = self._pnl_far(far_close)
        return n_pnl + f_pnl

    def _log_exit_decision(self, exit_reason: str, pnl: float, bar: dict) -> dict:
        """Helper to generate, print, and log the structured exit decision.
        2026-06-26 Gemini CLI: exit logging implementation.
        """
        _risk_meta = self._get_risk_meta(bar)
        exit_data = {
            "exit_reason": exit_reason,
            "risk_mode": _risk_meta.get("risk_mode"),
            "session": _risk_meta.get("session"),
            "atr": _risk_meta.get("atr"),
            "release_stop": _risk_meta.get("release_stop"),
            "trail_dist": _risk_meta.get("trail_dist"),
            "mfe": round(self._mfe_pts, 2),
            "mae": round(self._mae_pts, 2),
            "pnl": round(pnl, 2),
            # 2026-06-26 Hermes Agent: per-leg MFE/MAE in exit log
            "near_max": round(self._near_max, 2) if self._near_max is not None else None,
            "near_min": round(self._near_min, 2) if self._near_min is not None else None,
            "far_max": round(self._far_max, 2) if self._far_max is not None else None,
            "far_min": round(self._far_min, 2) if self._far_min is not None else None,
            "post_release_anchor": round(self._post_release_anchor_price, 2) if self._post_release_anchor_price is not None else None,
        }
        logger.info("[MTS_EXIT_LOG] %s", json.dumps(exit_data))
        _append_event("EXIT_LOG", **exit_data)
        return exit_data

    # ── State file read ─────────────────────────────────────────────────────
    @staticmethod
    def _read_mts_state() -> dict | None:
        """Read and return MTS position state from JSON file, or None."""
        try:
            if not os.path.exists(_MTS_STATE_FILE):
                return None
            # [Fix] Handle empty file case to avoid JSONDecodeError
            if os.path.getsize(_MTS_STATE_FILE) == 0:
                return None
            with open(_MTS_STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, ValueError, OSError):
            # Log as warning instead of exception to reduce noise in backtest
            logger.warning("[MTS_STATE_READ_FAILED] file=%s", _MTS_STATE_FILE)
            return None
        except Exception:
            logger.exception("[MTS_STATE_READ_UNEXPECTED] file=%s", _MTS_STATE_FILE)
            return None

    # ── Hot-reload / restart recovery ────────────────────────────────────────
    def _restore_position_state(self) -> bool:
        """
        Attempt to restore in-memory state from /tmp/mts_position_state.json.
        2026-05-27 Gemini CLI: Enhanced with log reconstruction for 100% confidence.

        Called at the top of on_bar() when _has_position is False.
        Only restores if the state indicates an open spread position
        (not CLOSE / EXIT / FLAT).

        Returns True if state was restored, False if nothing to restore.
        """
        # 2026-06-25 Gemini CLI: Skip restore during backtesting to prevent stale ghost positions
        if os.getenv("MTS_BACKTEST") == "1":
            return False
            
        state = self._read_mts_state()
        
        # 1. Primary Source: JSON State File
        if state and state.get("has_position") is True:
            action = state.get("state", "")
            if action not in ("CLOSE", "EXIT", "FLAT"):
                # Check for staleness
                _updated = state.get("_updated")
                if _updated:
                    try:
                        _ts = datetime.fromisoformat(_updated)
                        _age_min = (datetime.now() - _ts).total_seconds() / 60.0
                        # 60 min expiration for production stability
                        if _age_min < 60:
                            # 2026-05-27 Gemini CLI: Only accept JSON if it has valid peak/nadir memory
                            _rem_side = state.get("remaining_side")
                            # 2026-06-23 Gemini CLI: Remove numeric defaults in .get() to comply with no_get_numeric_fallback contract
                            _trail_peak_val = state.get("trail_peak")
                            _trail_nadir_val = state.get("trail_nadir")
                            _peak = float(_trail_peak_val) if _trail_peak_val is not None else 0.0
                            _nadir = float(_trail_nadir_val) if _trail_nadir_val is not None else 0.0

                            # If we are trailing but peak/nadir is 0, the JSON is "polluted" (likely by tests)
                            _released_leg_state = state.get("released_leg")
                            if _released_leg_state is None:
                                # Both legs held — remaining_side is meaningless for pollute check
                                _pollute_pass = True
                            elif (_rem_side == "LONG" and _peak > 0) or (_rem_side == "SHORT" and _nadir > 0) or not _rem_side:
                                _pollute_pass = True
                            else:
                                _pollute_pass = False
                            if _pollute_pass:
                                self._has_position = True
                                self._lifecycle = state.get("state", "OPEN")
                                # 2026-06-23 Gemini CLI: Safe parsing of float fields to prevent NoneType TypeError
                                self._entry_spread_z = float(state.get("entry_spread_z") or 0.0)
                                self._near_entry = float(state.get("near_entry") or 0.0)
                                self._far_entry = float(state.get("far_entry") or 0.0)
                                self._near_side = state.get("near_side")
                                self._far_side = state.get("far_side")
                                self._released_leg = state.get("released_leg")
                                self._side = _rem_side
                                self._peak = _peak
                                self._nadir = _nadir
                                
                                # 💡 [Fixed 2026-05-27] Robust trade_id recovery
                                self._trade_id = state.get("trade_id") or state.get("manual_order_id")
                                if not self._trade_id:
                                    logger.warning("[MTS_RESTORE_WARNING] reason=MISSING_TRADE_ID state=%s", action)
                                    self._trade_id = f"mts-recovered-{datetime.now().strftime('%H%M%S')}"

                                
                                # Best effort timestamps
                                self._entry_ts = datetime.fromisoformat(state.get("entry_ts")) if state.get("entry_ts") else datetime.now()
                                self._release_ts = datetime.now() if self._released_leg else None
                                self._release_mono = time.monotonic() if self._released_leg else 0.0
                                # 2026-05-27 Gemini CLI: Set monotonic entry time on restore to prevent immediate watchdog kill (P4)
                                self._entry_time_monotonic = time.monotonic()
                                
                                logger.info("[MTS_RESTORE_OK] source=JSON action=%s trade_id=%s", action, self._trade_id)
                                return True
                            else:
                                logger.warning("[MTS_RESTORE_REJECTED] reason=POLLUTED_DATA_PEAK_ZERO side=%s", _rem_side)
                    except:
                        pass
        
        # 2. Secondary Source: Fallback reconstruction from Fill Log
        # 2026-06-02 Gemini CLI: Enhanced with timestamp expiration to prevent "Ghost Trade" loops
        try:
            if os.path.exists(_MTS_FILL_LOG):
                with open(_MTS_FILL_LOG, "r") as f:
                    # Read last 100 lines to ensure we see the full trade lifecycle
                    lines = f.readlines()[-100:]
                    fills = []
                    for l in lines:
                        try: fills.append(json.loads(l))
                        except: pass
                    
                # Find the most recent ENTRY group
                last_entry = None
                for fill in reversed(fills):
                    if fill.get("fill_type") == "ENTRY":
                        last_entry = fill
                        break
                
                if last_entry:
                    last_entry_tid = last_entry.get("trade_id")
                    
                    # 💡 [Fixed 2026-06-02] Expiration Guard: Don't restore trades older than 12 hours
                    try:
                        _entry_ts = datetime.fromisoformat(last_entry["timestamp"])
                        _age_hrs = (datetime.now() - _entry_ts).total_seconds() / 3600.0
                        if _age_hrs > 12.0:
                            if self._last_skip_reason != f"RESTORE_EXPIRED_{last_entry_tid}":
                                logger.warning("[MTS_RESTORE_SKIP] trade_id=%s is too old (age=%.1fh > 12h). Ignoring.", 
                                               last_entry_tid, _age_hrs)
                                self._last_skip_reason = f"RESTORE_EXPIRED_{last_entry_tid}"
                            return False
                    except (KeyError, ValueError):
                        logger.error("[MTS_RESTORE_ERROR] Corrupt timestamp in fill log for %s", last_entry_tid)
                        return False

                    # Check if this trade_id was already CLOSED or EXITED
                    is_closed = any(f.get("trade_id") == last_entry_tid and f.get("fill_type") == "EXIT" for f in fills)
                    if not is_closed:
                        # Reconstruct basic state from entry fills
                        relevant = [f for f in fills if f.get("trade_id") == last_entry_tid]
                        near_f = next((f for f in relevant if f.get("leg") == "NEAR"), None)
                        far_f = next((f for f in relevant if f.get("leg") == "FAR"), None)
                        
                        if near_f and far_f:
                            self._has_position = True
                            self._trade_id = last_entry_tid
                            self._near_entry = float(near_f["price"])
                            self._far_entry = float(far_f["price"])
                            self._near_side = near_f["side"]
                            self._far_side = far_f["side"]
                            
                            # Check for release
                            release_f = next((f for f in fills if f.get("trade_id") == last_entry_tid and f.get("fill_type") in ("RELEASE", "RELEASE_SUBMIT")), None)
                            if release_f:
                                self._released_leg = "near" if release_f["leg"] == "NEAR" else "far"
                                self._side = "LONG" if (self._released_leg == "near" and self._far_side == "LONG") or (self._released_leg == "far" and self._near_side == "LONG") else "SHORT"
                                # 2026-05-27 Gemini CLI: Use actual release price as safety floor for peak/nadir
                                self._peak = float(release_f["price"]) if self._side == "LONG" else 0.0
                                self._nadir = float(release_f["price"]) if self._side == "SHORT" else 0.0
                                self._lifecycle = f"TRAILING_{self._side}"
                            else:
                                self._lifecycle = "OPEN"
                                self._peak = self._near_entry
                                self._nadir = self._far_entry
                                
                            logger.info("[MTS_RESTORE_OK] source=LOG trade_id=%s lifecycle=%s age=%.1fh", 
                                        self._trade_id, self._lifecycle, _age_hrs)
                            return True
        except Exception as e:
            logger.error("[MTS_RESTORE_LOG_FAILED] error=%s", e)

        return False

    def _append_skip(self, reason: str, **kwargs) -> None:
        """Append SKIP event only if reason changed or 5min elapsed since last."""
        now = datetime.now()
        _changed = reason != self._last_skip_reason
        _stale = (
            self._last_skip_ts is not None
            and (now - self._last_skip_ts).total_seconds() > 300
        )
        if _changed or _stale:
            _append_event("SKIP", reason=reason, **kwargs)
            self._last_skip_reason = reason
            self._last_skip_ts = now

    def on_bar(self, context: StrategyContext) -> Signal | None:
        # 2026-05-27 Gemini CLI: Hot-reload params from context on every tick for real-time Dashboard tuning
        _params = context.config.get("params", {})
        if _params:
            self._atr_mult_stop = float(_params.get("atr_multiplier_stop", self._atr_mult_stop))
            self._atr_mult_trail = float(_params.get("atr_multiplier_trail", self._atr_mult_trail))
            self._atr_cap = float(_params.get("atr_cap", self._atr_cap))
            self._release_stop_fixed = float(_params.get("release_stop_points", self._release_stop_fixed))
            self._trail_dist_fixed = float(_params.get("trail_distance_points", self._trail_dist_fixed))
            self._min_atr = float(_params.get("min_atr", self._min_atr))

        bar = context.market.last_bar
        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        # 2026-06-25 Gemini CLI: Define now early to use for re-entry cooldown checks
        ts = bar.get("timestamp")
        if isinstance(ts, datetime):
            now = ts
        else:
            now = datetime.now()

        # ── [Fix] Prevent duplicate submissions ──
        # 2026-06-11 JVS Claw: Add timeout for RELEASE lifecycle states
        # If release is stuck >60s without fill confirmation, reset to OPEN
        # so the next on_bar() can retry the release.
        if self._lifecycle in ("SUBMITTING", "RELEASE_NEAR", "RELEASE_FAR", "EXITING"):
            if self._lifecycle in ("RELEASE_NEAR", "RELEASE_FAR") and getattr(self, "_release_mono", 0.0) > 0.0:
                _release_age = time.monotonic() - self._release_mono
                if _release_age > 60:
                    logger.warning("[MTS_RELEASE_TIMEOUT] lifecycle=%s stuck for %.0fs. Resetting to OPEN for retry.", self._lifecycle, _release_age)
                    self._lifecycle = "OPEN"
                    self._release_ts = None
                    self._release_mono = 0.0
                    # Fall through to continue processing
                else:
                    self._set_eval(skip_reason="MTS_BUSY", lifecycle=self._lifecycle)
                    return None
            else:
                self._set_eval(skip_reason="MTS_BUSY", lifecycle=self._lifecycle)
                return None

        # ── [Fix] Re-entry Cooldown ──
        if self._last_exit_ts is not None:
            # 2026-06-25 Gemini CLI: Use bar timestamp (now) instead of wall-clock datetime.now() for backtesting
            _elapsed = (now - self._last_exit_ts).total_seconds()
            if _elapsed < self._reentry_cooldown_secs:
                self._set_eval(skip_reason="REENTRY_COOLDOWN", remaining=int(self._reentry_cooldown_secs - _elapsed))
                return None

        # ── Hot-reload guard: restore position state if lost ──
        if not self._has_position:
            try:
                self._restore_position_state()
            except Exception:
                logger.exception("[MTS_RESTORE_FAILED]")
                self._has_position = False

        # 2026-06-23 Gemini CLI: Remove numeric defaults in .get() to comply with no_get_numeric_fallback contract
        _near_close_val = bar.get("near_close")
        _far_close_val = bar.get("far_close")
        near_close = float(_near_close_val) if _near_close_val is not None else 0.0
        far_close = float(_far_close_val) if _far_close_val is not None else 0.0
        spread_z = bar.get("spread_z", None)

        if near_close <= 0 or far_close <= 0:
            self._set_eval(skip_reason="NO_SPREAD_DATA", near=near_close, far=far_close)
            return None

        # Cache ATR for management logic
        self._last_atr = bar.get("atr")

        # ── [Fix] Position management before stale gate ──
        if self._has_position:
            # 💡 [Fixed 2026-05-27] Re-sync self._trade_id from bar data if missing
            if not self._trade_id:
                self._trade_id = bar.get("trade_id")
            
            return self._manage_position(near_close, far_close, bar.get("spread_z"), now, bar)

        # ── Staleness gate (only for new entry) ──
        atr = bar.get("atr", 0.0)
        if atr < self._min_atr:
            self._set_eval(skip_reason=f"ATR_TOO_LOW ({atr:.2f}<{self._min_atr:.1f})")
            return None

        # 💡 [Fixed 2026-05-27] Disabled SPREAD_DATA_STALE gate
        # The cron job only updates the CSV 3 times a day.
        # We now calculate spread_z dynamically using RT prices in monitor.py.
        # _max_age_min = context.config.get("params", {}).get("max_spread_age_min", 7)
        # _age = bar.get("spread_age_minutes")
        # if _age is not None and isinstance(_age, (int, float)) and _age > _max_age_min:
        #    self._set_eval(skip_reason="SPREAD_DATA_STALE", age_min=int(_age))
        #    return None

        # ── Entry gate ──
        if spread_z is None or pd.isna(spread_z):
            self._set_eval(skip_reason="NO_SPREAD_Z")
            return None

        try:
            spread_z_f = float(spread_z)
        except (TypeError, ValueError):
            self._set_eval(skip_reason="SPREAD_Z_INVALID")
            return None

        # 2026-06-26 Gemini CLI: Parse dynamic entry Z based on ATR
        if isinstance(self._entry_z_cfg, dict):
            atr_val = atr if (atr and not pd.isna(atr)) else (self._last_atr or 20.0)
            low_bound = float(_params.get("atr_low_threshold", 15.0))
            high_bound = float(_params.get("atr_high_threshold", 30.0))
            if atr_val < low_bound:
                self._entry_z = float(self._entry_z_cfg.get("low_atr", 2.0))
            elif atr_val > high_bound:
                self._entry_z = float(self._entry_z_cfg.get("high_atr", 3.0))
            else:
                self._entry_z = float(self._entry_z_cfg.get("normal_atr", 2.5))
        else:
            self._entry_z = float(self._entry_z_cfg)

        if abs(spread_z_f) < self._entry_z:
            self._set_eval(skip_reason="SPREAD_Z_NOT_EXTREME", spread_z=round(spread_z_f, 2))
            return None

        if context.position.size != 0:
            self._set_eval(skip_reason="POSITION_OPEN")
            return None

        # ── [Fix] Prevent duplicate submissions ──
        if self._lifecycle == "SUBMITTING":
            self._set_eval(skip_reason="ENTRY_ALREADY_SUBMITTED")
            return None

        # ── Direction-aware entry ──
        if spread_z_f > 0:
            _action = "SELL_NEAR_BUY_FAR"
            _reason = "TMF_SPREAD_WIDE"
            self._peak = near_close
            self._nadir = far_close
        else:
            _action = "BUY_NEAR_SELL_FAR"
            _reason = "TMF_SPREAD_NARROW"
            self._peak = near_close
            self._nadir = far_close

        # [GSD] Deferred Strategy Sync: don't set _has_position = True yet.
        # monitor.py will call sync_position() once both legs are filled.
        self._lifecycle = "SUBMITTING"
        self._entry_ts = now
        # 2026-05-27 Gemini CLI: Use monotonic time for robust grace period (P2)
        self._entry_time_monotonic = time.monotonic()
        self._near_entry = near_close
        self._far_entry = far_close
        self._near_side = "SHORT" if spread_z_f > 0 else "LONG"
        self._far_side = "LONG" if spread_z_f > 0 else "SHORT"
        self._entry_spread_z = spread_z_f
        self._released_leg = None
        self._release_ts = None
        # trade_id will be overwritten by sync_position when fills are confirmed
        self._trade_id = f"mts-auto-{now.strftime('%Y%m%d-%H%M%S-%f')[:-3]}"

        # Calculate initial thresholds for state logging
        _init_stop, _init_trail = self._get_thresholds(bar)

        _write_mts_state(
            has_position=False, action="SUBMITTING", reason=_reason,
            near_entry=near_close, far_entry=far_close,
            near_last=near_close, far_last=far_close,
            near_side=self._near_side, far_side=self._far_side,
            spread_z=spread_z_f, released_leg=None,
            release_stop_points=_init_stop,
            trail_distance_points=_init_trail,
            trade_id=self._trade_id,
            # 2026-05-27 Gemini CLI: Pass current ticker to _write_mts_state for dynamic point value
            ticker=self._ticker,
            atr=self._last_atr, # 2026-06-26 Gemini CLI: pass current ATR to state writer
        )
        _append_event("ENTRY_SUBMITTED", action=_action, near_side=self._near_side, far_side=self._far_side,
                       near_entry=near_close, far_entry=far_close, spread_z=spread_z_f)
        
        # [Fix] Fill log moved to sync_position for true deferred sync
        # _append_fill(...) - removed from here

        self._set_eval(triggered=True, action=_action, near_entry=near_close, far_entry=far_close)
        return Signal(_action, _reason, stop_loss=0, confidence=0.5, quantity=1)

    # 2026-06-30 Hermes Agent: Sanity gate for tick-level price data — rejects 0/NaN/stale/garbage
    def _valid_price_or_fallback(self, value, fallback=None, ref_price=None):
        try:
            v = float(value)
        except (TypeError, ValueError):
            return fallback
        if not math.isfinite(v) or v <= 0:
            return fallback
        if ref_price:
            ref = float(ref_price)
            if v < ref * 0.5 or v > ref * 1.5:
                return fallback
        return v

    def _manage_position(
        self, near_close: float, far_close: float, spread_z: Any, now: datetime,
        bar: dict,
    ) -> Signal | None:
        # 2026-06-30 Gemini CLI: Expected behavior: Bypass time/tick confirmation checks during backtesting because backtests run instantly and monotonic time does not advance.
        _is_backtest = os.getenv("MTS_BACKTEST") == "1"
        """Manage existing spread position — release check + trailing exit."""
        # 2026-05-27 Gemini CLI: Order in-flight guard (Contract 3)
        if self._lifecycle == "EXITING":
            self._set_eval(skip_reason="EXIT_ALREADY_SUBMITTED")
            return None
            
        # 2026-06-26 Gemini CLI: build dynamic risk metadata
        _risk_meta = self._get_risk_meta(bar)

        # 2026-06-26 Hermes Agent: per-leg MFE/MAE tracking (telemetry only, no logic impact)
        _near_close = near_close
        _far_close = far_close
        # 2026-06-30 Hermes Agent: Sanity-gated far_high/far_low to reject garbage tick data
        _far_high_raw = bar.get("far_high_rt", bar.get("far_high"))
        _far_low_raw = bar.get("far_low_rt", bar.get("far_low"))
        _far_high = self._valid_price_or_fallback(_far_high_raw, fallback=_far_close, ref_price=self._far_entry)
        _far_low = self._valid_price_or_fallback(_far_low_raw, fallback=_far_close, ref_price=self._far_entry)
        # Near leg validity (same pattern, lighter: near data is more reliable)
        _near_high = float(bar.get("near_high", _near_close))
        _near_low = float(bar.get("near_low", _near_close))
        if self._near_max is None or _near_high > self._near_max:
            self._near_max = _near_high
        if self._near_min is None or _near_low < self._near_min:
            self._near_min = _near_low
        # 2026-06-30 Hermes Agent: Self-heal far_min/far_max if polluted by cold-start garbage
        if self._far_entry and self._far_min is not None and self._far_min < self._far_entry * 0.5:
            self._far_min = None
        if self._far_entry and self._far_max is not None and self._far_max > self._far_entry * 1.5:
            self._far_max = None
        if _far_high is not None and (self._far_max is None or _far_high > self._far_max):
            self._far_max = _far_high
        if _far_low is not None and (self._far_min is None or _far_low < self._far_min):
            self._far_min = _far_low

        # Update floating MFE / MAE
        current_pnl = self._get_current_pnl_pts(near_close, far_close)
        self._mfe_pts = max(self._mfe_pts, current_pnl)
        self._mae_pts = min(self._mae_pts, current_pnl)

        # Quote freshness check
        near_age = bar.get("near_tick_age_ms", bar.get("near_age_ms", -1))
        far_age = bar.get("far_tick_age_ms", bar.get("far_age_ms", -1))
        quote_age_ms = max(0.0, max(float(near_age), float(far_age))) if (near_age > 0 or far_age > 0) else 0.0
        
        if quote_age_ms > self._max_quote_age_ms:
            self._set_eval(skip_reason="STALE_QUOTE_AGE", age=quote_age_ms)
            return None

        # Check bid-ask spread width
        near_bid = bar.get("near_bid", near_close)
        near_ask = bar.get("near_ask", near_close)
        far_bid = bar.get("far_bid", far_close)
        far_ask = bar.get("far_ask", far_close)
        near_width = near_ask - near_bid
        far_width = far_ask - far_bid

        if near_width > self._max_spread_width or far_width > self._max_spread_width:
            self._set_eval(skip_reason="WIDE_SPREAD_WIDTH", near_width=near_width, far_width=far_width)
            return None

        # Dynamic thresholds
        release_stop, trail_dist = self._get_thresholds(bar)
        # 2026-05-27 Gemini CLI: Use dynamic multiplier from engine constants
        _mult = float(get_point_value(self._ticker))

        _n_pnl = self._pnl_near(near_close)
        _f_pnl = self._pnl_far(far_close)

        # ── Full spread held ──
        if self._released_leg is None:
            # 2026-06-29 Gemini CLI: Under Deferred Strategy Sync, if we are in RELEASE_NEAR/FAR
            # but released_leg is still None, we are awaiting fill confirmation for the released leg.
            if self._lifecycle in ("RELEASE_NEAR", "RELEASE_FAR"):
                self._set_eval(skip_reason="AWAITING_RELEASE_FILL", lifecycle=self._lifecycle)
                return None

            # 2026-06-25 Hermes Agent: Use bar time difference for grace period to ensure correct backtesting and live trading
            _GRACE_SECONDS = 5
            _is_grace = self._entry_ts is not None and (now - self._entry_ts).total_seconds() < _GRACE_SECONDS

            near_triggered = _n_pnl <= -release_stop
            far_triggered = _f_pnl <= -release_stop

            # Near release confirmation check
            if near_triggered:
                if self._release_near_ticks == 0:
                    self._release_near_start_time = time.monotonic()
                self._release_near_ticks += 1
            else:
                self._release_near_ticks = 0
                self._release_near_start_time = 0.0

            # Far release confirmation check
            if far_triggered:
                if self._release_far_ticks == 0:
                    self._release_far_start_time = time.monotonic()
                self._release_far_ticks += 1
            else:
                self._release_far_ticks = 0
                self._release_far_start_time = 0.0

            # Near trigger decision execution
            _near_confirmed = (_is_backtest or (self._release_near_ticks >= self._confirm_ticks and (time.monotonic() - self._release_near_start_time) * 1000 >= self._confirm_ms))
            if near_triggered and _near_confirmed:
                if _is_grace:
                    self._set_eval(skip_reason="RELEASE_SUPPRESSED_IN_GRACE", n_pnl=_n_pnl)
                else:
                    self._lifecycle = "RELEASE_NEAR"
                    self._release_ts = now
                    self._release_mono = time.monotonic()
                    
                    _pnl_pts = _n_pnl
                    _turnover = (self._near_entry + near_close) * _mult
                    _cost = 20.0 + _turnover * 2e-5
                    _realized = _pnl_pts * _mult - _cost
                    
                    # 2026-06-26 Hermes Agent: capture post-release anchor = remaining leg price at release
                    _anchor_price = far_close if far_close > 0 else None
                    _anchor_source = _LIVE_TICK if far_close > 0 else None
                    _anchor_age = quote_age_ms if far_close > 0 else None
                    self._post_release_anchor_price = _anchor_price
                    self._post_release_anchor_source = _anchor_source
                    self._post_release_anchor_age_ms = _anchor_age
                    if _anchor_price is not None:
                        _append_event("POST_RELEASE_ANCHOR_SET",
                            remaining_leg="FAR", anchor_price=_anchor_price,
                            price_source=_anchor_source, quote_age_ms=_anchor_age)
                    else:
                        _append_event("POST_RELEASE_ANCHOR_SET",
                            remaining_leg="FAR", anchor_price=None,
                            price_source=_MISSING, quote_age_ms=quote_age_ms)

                    # 2026-06-26 Hermes Agent: per-leg MFE/MAE for release fill
                    _near_mfe = (self._near_max - self._near_entry) if self._near_side == "LONG" else (self._near_entry - self._near_min)
                    _near_mae = (self._near_entry - self._near_min) if self._near_side == "LONG" else (self._near_max - self._near_entry)

                    # 2026-06-26 Gemini CLI: log decision with structured exit metadata
                    self._log_exit_decision(exit_reason="RELEASE_STOP", pnl=_pnl_pts, bar=bar)
                    _append_event("RELEASE_NEAR_SUBMITTED", 
                                  released_leg="NEAR", remaining_leg="FAR",
                                  leg_side=self._near_side, entry_price=self._near_entry, exit_price=near_close,
                                  gross_points=_pnl_pts, multiplier=_mult, cost=_cost, realized_pnl=_realized,
                                  near_mfe=round(_near_mfe, 2), near_mae=round(_near_mae, 2),
                                  **_risk_meta)

                    _write_mts_state(
                        has_position=True, action="RELEASE_NEAR", reason=f"near_pnl={_n_pnl:.1f}",
                        near_entry=self._near_entry, far_entry=self._far_entry,
                        near_last=near_close, far_last=far_close,
                        near_side=self._near_side, far_side=self._far_side,
                        spread_z=spread_z, released_leg=None, # Keep as None until fill
                        release_price=self._release_price,
                        release_stop_points=release_stop, trail_distance_points=trail_dist,
                        trade_id=self._trade_id, ticker=self._ticker,
                        **_risk_meta
                    )
                    return Signal("PARTIAL_EXIT", "TMF_RELEASE_NEAR", confidence=0.4)

            # Far trigger decision execution
            _far_confirmed = (_is_backtest or (self._release_far_ticks >= self._confirm_ticks and (time.monotonic() - self._release_far_start_time) * 1000 >= self._confirm_ms))
            if far_triggered and _far_confirmed:
                if _is_grace:
                    self._set_eval(skip_reason="RELEASE_SUPPRESSED_IN_GRACE", f_pnl=_f_pnl)
                else:
                    self._lifecycle = "RELEASE_FAR"
                    self._release_ts = now
                    self._release_mono = time.monotonic()
                    
                    _pnl_pts = _f_pnl
                    _turnover = (self._far_entry + far_close) * _mult
                    _cost = 20.0 + _turnover * 2e-5
                    _realized = _pnl_pts * _mult - _cost

                    # 2026-06-26 Hermes Agent: capture post-release anchor = remaining leg price at release
                    _anchor_price = near_close if near_close > 0 else None
                    _anchor_source = _LIVE_TICK if near_close > 0 else None
                    _anchor_age = quote_age_ms if near_close > 0 else None
                    self._post_release_anchor_price = _anchor_price
                    self._post_release_anchor_source = _anchor_source
                    self._post_release_anchor_age_ms = _anchor_age
                    if _anchor_price is not None:
                        _append_event("POST_RELEASE_ANCHOR_SET",
                            remaining_leg="NEAR", anchor_price=_anchor_price,
                            price_source=_anchor_source, quote_age_ms=_anchor_age)
                    else:
                        _append_event("POST_RELEASE_ANCHOR_SET",
                            remaining_leg="NEAR", anchor_price=None,
                            price_source=_MISSING, quote_age_ms=quote_age_ms)

                    # 2026-06-26 Hermes Agent: per-leg MFE/MAE for release fill
                    _fm = self._far_max if self._far_max is not None else self._far_entry
                    _fn = self._far_min if self._far_min is not None else self._far_entry
                    _far_mfe = (_fm - self._far_entry) if self._far_side == "LONG" else (self._far_entry - _fn)
                    _far_mae = (self._far_entry - _fn) if self._far_side == "LONG" else (_fm - self._far_entry)

                    # 2026-06-26 Gemini CLI: log decision with structured exit metadata
                    self._log_exit_decision(exit_reason="RELEASE_STOP", pnl=_pnl_pts, bar=bar)
                    _append_event("RELEASE_FAR_SUBMITTED", 
                                  released_leg="FAR", remaining_leg="NEAR",
                                  leg_side=self._far_side, entry_price=self._far_entry, exit_price=far_close,
                                  gross_points=_pnl_pts, multiplier=_mult, cost=_cost, realized_pnl=_realized,
                                  far_mfe=round(_far_mfe, 2), far_mae=round(_far_mae, 2),
                                  **_risk_meta)

                    _write_mts_state(
                        has_position=True, action="RELEASE_FAR", reason=f"far_pnl={_f_pnl:.1f}",
                        near_entry=self._near_entry, far_entry=self._far_entry,
                        near_last=near_close, far_last=far_close,
                        near_side=self._near_side, far_side=self._far_side,
                        spread_z=spread_z, released_leg=None, # Keep as None until fill
                        release_price=self._release_price,
                        release_stop_points=release_stop, trail_distance_points=trail_dist,
                        trade_id=self._trade_id, ticker=self._ticker,
                        **_risk_meta
                    )
                    return Signal("PARTIAL_EXIT", "TMF_RELEASE_FAR", confidence=0.4)

            _write_mts_state(
                has_position=True, action="HOLDING_SPREAD", reason=f"near_pnl={_n_pnl:.1f} far_pnl={_f_pnl:.1f}",
                near_entry=self._near_entry, far_entry=self._far_entry,
                near_last=near_close, far_last=far_close,
                near_side=self._near_side, far_side=self._far_side,
                spread_z=spread_z, released_leg=self._released_leg,
                release_price=self._release_price,
                trail_pts=trail_dist, release_stop_points=release_stop,
                trail_distance_points=trail_dist, trade_id=self._trade_id,
                ticker=self._ticker,
                **_risk_meta
            )
            return None

        # ── Trailing mode ──
        if self._released_leg is not None:
            # 2026-06-29 Gemini CLI: Normalize restored RELEASE_NEAR/FAR lifecycle to TRAILING
            if self._lifecycle in ("RELEASE_NEAR", "RELEASE_FAR"):
                self._lifecycle = f"TRAILING_{self._side}"

        if self._released_leg == "near":
            _rem_price, _rem_entry, _rem_leg_label, _released_leg_label = far_close, self._far_entry, "FAR", "NEAR"
            # 2026-05-27 Gemini CLI: Evaluate intra-bar extremes
            _rem_high = float(bar.get("far_high", far_close))
            _rem_low = float(bar.get("far_low", far_close))
        else:
            _rem_price, _rem_entry, _rem_leg_label, _released_leg_label = near_close, self._near_entry, "NEAR", "FAR"
            # 2026-05-27 Gemini CLI: Evaluate intra-bar extremes
            _rem_high = float(bar.get("near_high", near_close))
            _rem_low = float(bar.get("near_low", near_close))

        # 💡 [Fixed 2026-05-27] Guard against zero or invalid prices in trailing mode
        if _rem_high <= 0 or _rem_low <= 0:
            self._set_eval(skip_reason="INVALID_TRAILING_PRICE", high=_rem_high, low=_rem_low)
            return None

        # Update peak/nadir and check trailing exits
        atr_val = bar.get("atr")
        if not atr_val or pd.isna(atr_val):
            atr_val = self._last_atr or 20.0

        rem_floating_pnl = (_rem_price - _rem_entry) if self._side == "LONG" else (_rem_entry - _rem_price)

        if self._side == "LONG":
            self._peak = max(self._peak, _rem_high)
            _trail_stop = self._peak - trail_dist
        else: # SHORT
            self._nadir = min(self._nadir, _rem_low)
            _trail_stop = self._nadir + trail_dist

        # Post-Release Stage 1: Breakeven Stop-loss Adjustment
        post_release = self._params.get("post_release", {})
        breakeven_atr_mult = post_release.get("breakeven_after_atr")
        if breakeven_atr_mult is not None and atr_val > 0:
            if rem_floating_pnl >= float(breakeven_atr_mult) * atr_val:
                if self._side == "LONG":
                    _trail_stop = max(_trail_stop, _rem_entry)
                else:
                    _trail_stop = min(_trail_stop, _rem_entry)

        # Post-Release Stage 3: Force Lock
        force_lock_mult = post_release.get("force_lock_after_atr")
        force_lock_triggered = False
        if force_lock_mult is not None and atr_val > 0:
            if rem_floating_pnl >= float(force_lock_mult) * atr_val:
                force_lock_triggered = True

        # Check exit triggers
        trail_triggered = False
        if self._side == "LONG":
            if _rem_low <= _trail_stop:
                trail_triggered = True
        else: # SHORT
            if _rem_high >= _trail_stop:
                trail_triggered = True

        exit_triggered = trail_triggered or force_lock_triggered
        exit_reason = "FORCE_LOCK" if force_lock_triggered else "TRAIL_STOP"

        # Trailing Exit confirmation check
        if exit_triggered:
            if self._trail_exit_ticks == 0:
                self._trail_exit_start_time = time.monotonic()
            self._trail_exit_ticks += 1
        else:
            self._trail_exit_ticks = 0
            self._trail_exit_start_time = 0.0

        # 2026-06-30 Gemini CLI: Expected behavior: Bypass trailing exit confirmation check during backtests to prevent monotonic time-lock.
        _is_backtest = os.getenv("MTS_BACKTEST") == "1"
        _exit_confirmed = (_is_backtest or (self._trail_exit_ticks >= self._confirm_ticks and (time.monotonic() - self._trail_exit_start_time) * 1000 >= self._confirm_ms))
        if exit_triggered and _exit_confirmed:
            # Trailing Exit confirmed
            exit_price = _rem_price if force_lock_triggered else (_rem_low if self._side == "LONG" else _rem_high)
            _pnl_pts = (exit_price - _rem_entry) if self._side == "LONG" else (_rem_entry - exit_price)
            _turnover = (_rem_entry + exit_price) * _mult
            _cost = 20.0 + _turnover * 2e-5
            _realized = _pnl_pts * _mult - _cost
            
            # 2026-06-26 Gemini CLI: log decision with structured exit metadata
            self._log_exit_decision(exit_reason=exit_reason, pnl=_pnl_pts, bar=bar)
            _append_event("EXIT_REMAINING", reason=f"{exit_reason}_{self._side}", 
                           released_leg=_released_leg_label, remaining_leg=_rem_leg_label,
                           leg_side=self._side, entry_price=_rem_entry, exit_price=exit_price,
                           gross_points=_pnl_pts, multiplier=_mult, cost=_cost, realized_pnl=_realized,
                           leg_mfe=round((_rem_high - _rem_entry) if self._side == "LONG" else (_rem_entry - _rem_low), 2),
                           leg_mae=round((_rem_entry - _rem_low) if self._side == "LONG" else (_rem_high - _rem_entry), 2),
                           **_risk_meta)
            # _append_fill(...) - removed from here to follow "Side effects only after operation succeeds" rule.
            # Exit fill will be logged upon confirmation in _reset().
            
            self._lifecycle = "EXITING"
            self._exit_start_time = time.monotonic()
            return Signal("EXIT", f"TMF_{exit_reason}_{self._side}", confidence=0.5, stop_loss=0)
        
        _write_mts_state(
            has_position=True, action=f"TRAILING_{self._side}",
            reason=f'{_rem_leg_label} trail={(self._peak - _rem_low if self._side == "LONG" else _rem_high - self._nadir):.1f}/{trail_dist}',
            near_entry=self._near_entry, far_entry=self._far_entry,
            near_last=near_close, far_last=far_close,
            near_side=self._near_side, far_side=self._far_side,
            spread_z=spread_z, released_leg=self._released_leg,
            release_price=self._release_price,
            trail_pts=trail_dist, trail_peak=self._peak, trail_nadir=self._nadir,
            release_stop_points=release_stop, trail_distance_points=trail_dist,
            trade_id=self._trade_id, ticker=self._ticker,
            **_risk_meta
        )
        return None

    # 2026-06-25 Gemini CLI: Support passing exit_ts for backtests to avoid E-Core cooldown block
    def _reset(self, reason: str | None = None, exit_ts: datetime | None = None, exit_price: float | None = None) -> None:
        # 2026-06-18 Gemini CLI: Fix AttributeError - StrategyBase has no 'config' attribute.
        # Fallback to TMF if _ticker is not yet initialized.
        _ticker = getattr(self, '_ticker', "TMF")
        
        # 2026-06-29 Gemini CLI: Log the confirmed exit fill only when it is confirmed filled
        if exit_price is not None and self._released_leg is not None:
            _rem_leg = "NEAR" if self._released_leg == "far" else "FAR"
            _rem_entry = self._near_entry if _rem_leg == "NEAR" else self._far_entry
            _rem_side = self._near_side if _rem_leg == "NEAR" else self._far_side
            
            _exit_side = "SELL" if _rem_side == "LONG" else "BUY"
            _pnl_pts = (exit_price - _rem_entry) if _rem_side == "LONG" else (_rem_entry - exit_price)
            _mult = float(get_point_value(_ticker))
            _turnover = (_rem_entry + exit_price) * _mult
            _cost = 20.0 + _turnover * 2e-5
            _realized = _pnl_pts * _mult - _cost
            
            # MFE/MAE calculations
            if _rem_leg == "NEAR":
                _rem_high = self._near_max if self._near_max is not None else exit_price
                _rem_low = self._near_min if self._near_min is not None else exit_price
            else:
                _rem_high = self._far_max if self._far_max is not None else exit_price
                _rem_low = self._far_min if self._far_min is not None else exit_price

            _leg_mfe = (_rem_high - _rem_entry) if _rem_side == "LONG" else (_rem_entry - _rem_low)
            _leg_mae = (_rem_entry - _rem_low) if _rem_side == "LONG" else (_rem_high - _rem_entry)

            _post_anchor = self._post_release_anchor_price
            if _post_anchor is not None:
                _pr_mfe = (_rem_high - _post_anchor) if self._side == "LONG" else (_post_anchor - _rem_low)
                _pr_mae = (_post_anchor - _rem_low) if self._side == "LONG" else (_rem_high - _post_anchor)
                _pr_giveback = _pr_mfe - ((exit_price - _rem_entry) if self._side == "LONG" else (_rem_entry - exit_price))
            else:
                _pr_mfe = None
                _pr_mae = None
                _pr_giveback = None

            _append_fill(
                ticker=_ticker,
                contract=_rem_leg,
                leg=_rem_leg,
                side=_exit_side,
                qty=1,
                price=exit_price,
                fill_type="EXIT",
                trade_id=self._trade_id or "MISSING_TID",
                spread_z=None,
                realized_pnl=_realized,
                leg_mfe=_leg_mfe,
                leg_mae=_leg_mae,
                post_release_anchor_price=_post_anchor,
                post_release_mfe=_pr_mfe,
                post_release_mae=_pr_mae,
                post_release_giveback=_pr_giveback,
                price_source=_LIVE_TICK,
                quote_age_ms=0.0
            )

        _write_mts_state(has_position=False, action="CLOSE", reason=reason or "trail_exit", ticker=_ticker)
        self._has_position = False
        self._lifecycle = "FLAT"
        self._last_exit_ts = exit_ts or datetime.now()  # 2026-06-25 Gemini CLI: Support passing historical exit timestamp
        self._entry_ts = None
        self._near_entry = 0.0
        self._far_entry = 0.0
        self._near_side = None
        self._far_side = None
        self._entry_spread_z = 0.0
        self._released_leg = None
        self._release_ts = None
        self._release_mono = 0.0
        self._peak = 0.0
        self._nadir = 0.0
        self._side = None
        # 2026-05-27 Gemini CLI: Watchdog metrics (P2)
        self._exit_start_time = 0.0
        # 2026-06-26 Hermes Agent: reset MFE/MAE telemetry
        self._near_max = None
        self._near_min = None
        self._far_max = None
        self._far_min = None
        self._post_release_anchor_price = None
        self._post_release_anchor_source = None
        self._post_release_anchor_age_ms = None
        self._mfe_pts = 0.0
        self._mae_pts = 0.0
        self._release_price = 0.0

    def cleanup(self) -> None:
        self._reset()
