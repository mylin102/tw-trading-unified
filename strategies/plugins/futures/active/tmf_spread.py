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
_MTS_EVENT_LOG = "logs/mts_spread_events.jsonl"
_MTS_FILL_LOG = "logs/mts_trade_fills.jsonl"


def _append_event(event_type: str, **kwargs) -> None:
    """Append a lifecycle event to the MTS event ledger (append-only JSONL)."""
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


def _append_fill(ticker: str, contract: str, leg: str, side: str, qty: int,
                 price: float, fill_type: str, trade_id: str,
                 spread_z: float | None = None,
                 realized_pnl: float | None = None) -> None:
    """Append a trade fill record (append-only JSONL)."""
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
) -> None:
    """
    Write MTS position state JSON for dashboard consumption.
    Implements Field Level Protection:
    - Immutable: entry_prices, sides, trade_id, entry_ts
    - Mutable: last_prices, upl, trail_state, updated_at
    """
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
        _f_near_entry = near_entry if near_entry > 0 else float(existing.get("near_entry", 0))
        _f_far_entry = far_entry if far_entry > 0 else float(existing.get("far_entry", 0))
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
            "entry_spread_z": round(spread_z, 2) if spread_z != 0 else existing.get("entry_spread_z"),
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
            "spread_z": round(spread_z, 2),
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
            "_updated": datetime.now().isoformat(),
        }
        # 2026-05-29 Hermes Agent: fix typo _hb_state → state (NameError crash)
        _tmp_file = _MTS_STATE_FILE + ".tmp"
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
        self._entry_z = float(_params.get("entry_z", _ENTRY_Z))
        
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
        self._peak: float = 0.0  # for long trailing (highest)
        self._nadir: float = 0.0  # for short trailing (lowest)
        self._side: str | None = None  # "LONG" or "SHORT" for remaining leg (set on release)
        self._trade_id: str | None = None  # trade ID for fill ledger
        self._last_skip_reason: str | None = None  # dedup SKIP events
        self._last_skip_ts: datetime | None = None  # throttle SKIP events
        self._last_atr: float | None = None

    def _get_thresholds(self, bar: dict) -> tuple[float, float]:
        """Calculate dynamic thresholds based on ATR, or use fixed fallbacks."""
        atr = bar.get("atr")
        if atr and not pd.isna(atr) and atr > 0:
            stop = atr * self._atr_mult_stop
            trail = atr * self._atr_mult_trail
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
        self._entry_ts = datetime.now()
        # 2026-05-27 Gemini CLI: Use monotonic time for robust grace period (P2)
        self._entry_time_monotonic = time.monotonic()
        self._peak = near_entry
        self._nadir = far_entry

        # [GSD] Log confirmed fills and ENTRY event
        _append_fill(self._ticker, "NEAR", "NEAR", self._near_side, 1, near_entry, "ENTRY", trade_id, spread_z=entry_spread_z)
        _append_fill(self._ticker, "FAR", "FAR", self._far_side, 1, far_entry, "ENTRY", trade_id, spread_z=entry_spread_z)
        # 2026-05-27 Gemini CLI: Use dynamic multiplier for event logging
        _mult = get_point_value(self._ticker)
        # 2026-05-27 Gemini CLI: P1 & P2: Entry Snapshot Integrity & Grace Period
        # Include price source and age metadata in entry log.
        _near_src = kwargs.get("near_price_source", "UNKNOWN")
        _far_src = kwargs.get("far_price_source", "UNKNOWN")
        _near_age = kwargs.get("near_tick_age_ms", -1)
        _far_age = kwargs.get("far_tick_age_ms", -1)

        _append_event("ENTRY", action="SELL_NEAR_BUY_FAR" if self._near_side == "SHORT" else "BUY_NEAR_SELL_FAR", 
                       near_side=self._near_side, far_side=self._far_side,
                       near_entry=near_entry, far_entry=far_entry, spread_z=entry_spread_z, 
                       trade_id=trade_id, multiplier=_mult,
                       near_source=_near_src, far_source=_far_src, 
                       near_age_ms=_near_age, far_age_ms=_far_age)
    def sync_release(self, leg: str, price: float) -> None:
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
        
        # Ensure peak/nadir are primed with the release-time price of the REMAINING leg
        if self._side == "LONG": 
            self._peak = price
            self._nadir = 0.0
        else: 
            self._nadir = price
            self._peak = 0.0
            
        logger.info("[MTS_RELEASE_SYNC] leg_released=%s rem_side=%s price=%s lifecycle=%s trade_id=%s", 
                    leg, self._side, price, self._lifecycle, self._trade_id)

    def _pnl_far(self, far_close: float) -> float:
        if self._far_side == "LONG":
            return far_close - self._far_entry
        return self._far_entry - far_close  # Short far → profit when far drops

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
                            _peak = float(state.get("trail_peak", 0))
                            _nadir = float(state.get("trail_nadir", 0))

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
                                self._entry_spread_z = float(state.get("entry_spread_z", 0))
                                self._near_entry = float(state.get("near_entry", 0))
                                self._far_entry = float(state.get("far_entry", 0))
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

        # ── [Fix] Prevent duplicate submissions ──
        # 2026-06-11 JVS Claw: Add timeout for RELEASE lifecycle states
        # If release is stuck >60s without fill confirmation, reset to OPEN
        # so the next on_bar() can retry the release.
        if self._lifecycle in ("SUBMITTING", "RELEASE_NEAR", "RELEASE_FAR", "EXITING"):
            if self._lifecycle in ("RELEASE_NEAR", "RELEASE_FAR") and self._release_ts is not None:
                _release_age = (datetime.now() - self._release_ts).total_seconds()
                if _release_age > 60:
                    console.print(f"[bold yellow]⚠️ [MTS_RELEASE_TIMEOUT] lifecycle={self._lifecycle} stuck for {_release_age:.0f}s. Resetting to OPEN for retry.[/bold yellow]")
                    self._lifecycle = "OPEN"
                    self._release_ts = None
                    # Fall through to continue processing
                else:
                    self._set_eval(skip_reason="MTS_BUSY", lifecycle=self._lifecycle)
                    return None
            else:
                self._set_eval(skip_reason="MTS_BUSY", lifecycle=self._lifecycle)
                return None

        # ── [Fix] Re-entry Cooldown ──
        if self._last_exit_ts is not None:
            _elapsed = (datetime.now() - self._last_exit_ts).total_seconds()
            if _elapsed < self._reentry_cooldown_secs:
                self._set_eval(skip_reason="REENTRY_COOLDOWN", remaining=int(self._reentry_cooldown_secs - _elapsed))
                return None

        bar = context.market.last_bar
        
        # ── Hot-reload guard: restore position state if lost ──
        if not self._has_position:
            try:
                self._restore_position_state()
            except Exception:
                logger.exception("[MTS_RESTORE_FAILED]")
                self._has_position = False

        if not bar:
            self._set_eval(skip_reason="NO_BAR")
            return None

        near_close = float(bar.get("near_close", 0))
        far_close = float(bar.get("far_close", 0))
        spread_z = bar.get("spread_z", None)
        ts = bar.get("timestamp")
        if isinstance(ts, datetime):
            now = ts
        else:
            now = datetime.now()

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
        if spread_z is None:
            self._set_eval(skip_reason="NO_SPREAD_Z")
            return None

        try:
            spread_z_f = float(spread_z)
        except (TypeError, ValueError):
            self._set_eval(skip_reason="SPREAD_Z_INVALID")
            return None

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
        )
        _append_event("ENTRY_SUBMITTED", action=_action, near_side=self._near_side, far_side=self._far_side,
                       near_entry=near_close, far_entry=far_close, spread_z=spread_z_f)
        
        # [Fix] Fill log moved to sync_position for true deferred sync
        # _append_fill(...) - removed from here

        self._set_eval(triggered=True, action=_action, near_entry=near_close, far_entry=far_close)
        return Signal(_action, _reason, stop_loss=0, confidence=0.5, quantity=1)

    def _manage_position(
        self, near_close: float, far_close: float, spread_z: Any, now: datetime,
        bar: dict,
    ) -> Signal | None:
        """Manage existing spread position — release check + trailing exit."""
        # 2026-05-27 Gemini CLI: Order in-flight guard (Contract 3)
        if self._lifecycle == "EXITING":
            self._set_eval(skip_reason="EXIT_ALREADY_SUBMITTED")
            return None
            
        # Dynamic thresholds
        release_stop, trail_dist = self._get_thresholds(bar)
        # 2026-05-27 Gemini CLI: Use dynamic multiplier from engine constants
        _mult = float(get_point_value(self._ticker))

        _n_pnl = self._pnl_near(near_close)
        _f_pnl = self._pnl_far(far_close)

        # ── Full spread held ──
        if self._released_leg is None:
            # 2026-05-27 Gemini CLI: P2: Entry Grace Period (5s) using monotonic time
            # Suppress RELEASE triggers but allow EXIT (emergency guards)
            _GRACE_SECONDS = 5
            _is_grace = hasattr(self, "_entry_time_monotonic") and (time.monotonic() - self._entry_time_monotonic) < _GRACE_SECONDS

            if _n_pnl <= -release_stop:
                if _is_grace:
                    self._set_eval(skip_reason="RELEASE_SUPPRESSED_IN_GRACE", n_pnl=_n_pnl)
                else:
                    # 💡 [Fixed 2026-05-27] Deferred Sync for Release
                    # Set lifecycle to block re-submission, but DON'T set _released_leg yet.
                    # _released_leg will be set by sync_release() upon fill confirmation.
                    self._lifecycle = "RELEASE_NEAR"
                    self._release_ts = now
                    # We can't set self._side yet because we don't know the fill price,
                    # but we know it will be the far_side.
                    
                    _pnl_pts = _n_pnl
                    _turnover = (self._near_entry + near_close) * _mult
                    _cost = 20.0 + _turnover * 2e-5
                    _realized = _pnl_pts * _mult - _cost
                    
                    _append_event("RELEASE_NEAR_SUBMITTED", 
                                  released_leg="NEAR", remaining_leg="FAR",
                                  leg_side=self._near_side, entry_price=self._near_entry, exit_price=near_close,
                                  gross_points=_pnl_pts, multiplier=_mult, cost=_cost, realized_pnl=_realized)

                    _release_side = "BUY" if self._near_side == "SHORT" else "SELL"
                    _append_fill(self._ticker, "NEAR", "NEAR", _release_side, 1, near_close, "RELEASE_SUBMIT", 
                                 self._trade_id or "MISSING_TID", spread_z=float(spread_z) if spread_z is not None else None, 
                                 realized_pnl=_realized)
                    
                    _write_mts_state(
                        has_position=True, action="RELEASE_NEAR", reason=f"near_pnl={_n_pnl:.1f}",
                        near_entry=self._near_entry, far_entry=self._far_entry,
                        near_last=near_close, far_last=far_close,
                        near_side=self._near_side, far_side=self._far_side,
                        spread_z=spread_z, released_leg=None, # Keep as None until fill
                        release_stop_points=release_stop, trail_distance_points=trail_dist,
                        trade_id=self._trade_id, ticker=self._ticker,
                    )
                    return Signal("PARTIAL_EXIT", "TMF_RELEASE_NEAR", confidence=0.4)

            if _f_pnl <= -release_stop:
                if _is_grace:
                    self._set_eval(skip_reason="RELEASE_SUPPRESSED_IN_GRACE", f_pnl=_f_pnl)
                else:
                    # 💡 [Fixed 2026-05-27] Deferred Sync for Release
                    self._lifecycle = "RELEASE_FAR"
                    self._release_ts = now
                    
                    _pnl_pts = _f_pnl
                    _turnover = (self._far_entry + far_close) * _mult
                    _cost = 20.0 + _turnover * 2e-5
                    _realized = _pnl_pts * _mult - _cost

                    _append_event("RELEASE_FAR_SUBMITTED", 
                                  released_leg="FAR", remaining_leg="NEAR",
                                  leg_side=self._far_side, entry_price=self._far_entry, exit_price=far_close,
                                  gross_points=_pnl_pts, multiplier=_mult, cost=_cost, realized_pnl=_realized)

                    _release_side = "BUY" if self._far_side == "SHORT" else "SELL"
                    _append_fill(self._ticker, "FAR", "FAR", _release_side, 1, far_close, "RELEASE_SUBMIT", 
                                 self._trade_id or "MISSING_TID", spread_z=float(spread_z) if spread_z is not None else None, 
                                 realized_pnl=_realized)

                    _write_mts_state(
                        has_position=True, action="RELEASE_FAR", reason=f"far_pnl={_f_pnl:.1f}",
                        near_entry=self._near_entry, far_entry=self._far_entry,
                        near_last=near_close, far_last=far_close,
                        near_side=self._near_side, far_side=self._far_side,
                        spread_z=spread_z, released_leg=None, # Keep as None until fill
                        release_stop_points=release_stop, trail_distance_points=trail_dist,
                        trade_id=self._trade_id, ticker=self._ticker,
                    )
                    return Signal("PARTIAL_EXIT", "TMF_RELEASE_FAR", confidence=0.4)

            _write_mts_state(
                has_position=True, action="HOLDING_SPREAD", reason=f"near_pnl={_n_pnl:.1f} far_pnl={_f_pnl:.1f}",
                near_entry=self._near_entry, far_entry=self._far_entry,
                near_last=near_close, far_last=far_close,
                near_side=self._near_side, far_side=self._far_side,
                spread_z=spread_z, released_leg=self._released_leg,
                trail_pts=trail_dist, release_stop_points=release_stop,
                trail_distance_points=trail_dist, trade_id=self._trade_id,
                # 2026-05-27 Gemini CLI: Pass current ticker to _write_mts_state for dynamic point value
                ticker=self._ticker,
            )
            return None

        # ── Trailing mode ──
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

        if self._side == "LONG":
            self._peak = max(self._peak, _rem_high)
            trail_distance = self._peak - _rem_low
            if trail_distance >= trail_dist:
                _pnl_pts = (_rem_low - _rem_entry)
                _turnover = (_rem_entry + _rem_low) * _mult
                _cost = 20.0 + _turnover * 2e-5
                _realized = _pnl_pts * _mult - _cost
                _append_event("EXIT_REMAINING", reason="TRAIL_LONG", 
                              released_leg=_released_leg_label, remaining_leg=_rem_leg_label,
                              leg_side="LONG", entry_price=_rem_entry, exit_price=_rem_low,
                              gross_points=_pnl_pts, multiplier=_mult, cost=_cost, realized_pnl=_realized)
                _append_fill(self._ticker, _rem_leg_label, _rem_leg_label, "SELL", 1, _rem_low, "EXIT", 
                             self._trade_id or "MISSING_TID", spread_z=float(spread_z) if spread_z is not None else None, realized_pnl=_realized)
                # 2026-05-27 Gemini CLI: Change to EXITING state to prevent double submission and missing _side in monitor
                self._lifecycle = "EXITING"
                self._exit_start_time = time.monotonic()
                return Signal("EXIT", "TMF_TRAIL_EXIT_LONG", confidence=0.5, stop_loss=0)
        else: # SHORT
            self._nadir = min(self._nadir, _rem_low)
            trail_distance = _rem_high - self._nadir
            if trail_distance >= trail_dist:
                _pnl_pts = (_rem_entry - _rem_high)
                _turnover = (_rem_entry + _rem_high) * _mult
                _cost = 20.0 + _turnover * 2e-5
                _realized = _pnl_pts * _mult - _cost
                _append_event("EXIT_REMAINING", reason="TRAIL_SHORT", 
                              released_leg=_released_leg_label, remaining_leg=_rem_leg_label,
                              leg_side="SHORT", entry_price=_rem_entry, exit_price=_rem_high,
                              gross_points=_pnl_pts, multiplier=_mult, cost=_cost, realized_pnl=_realized)
                _append_fill(self._ticker, _rem_leg_label, _rem_leg_label, "BUY", 1, _rem_high, "EXIT", 
                             self._trade_id or "MISSING_TID", spread_z=float(spread_z) if spread_z is not None else None, realized_pnl=_realized)
                # 2026-05-27 Gemini CLI: Change to EXITING state to prevent double submission and missing _side in monitor
                self._lifecycle = "EXITING"
                self._exit_start_time = time.monotonic()
                return Signal("EXIT", "TMF_TRAIL_EXIT_SHORT", confidence=0.5, stop_loss=0)
        
        _write_mts_state(
            has_position=True, action=f"TRAILING_{self._side}",
            reason=f'{_rem_leg_label} trail={trail_distance:.1f}/{trail_dist}',
            near_entry=self._near_entry, far_entry=self._far_entry,
            near_last=near_close, far_last=far_close,
            near_side=self._near_side, far_side=self._far_side,
            spread_z=spread_z, released_leg=self._released_leg,
            trail_pts=trail_dist, trail_peak=self._peak, trail_nadir=self._nadir,
            release_stop_points=release_stop, trail_distance_points=trail_dist,
            # 2026-05-27 Gemini CLI: Pass current ticker to _write_mts_state for dynamic point value
            trade_id=self._trade_id, ticker=self._ticker,
        )
        return None

    def _reset(self, reason: str | None = None) -> None:
        # 2026-06-18 Gemini CLI: Fix AttributeError - StrategyBase has no 'config' attribute.
        # Fallback to TMF if _ticker is not yet initialized.
        _ticker = getattr(self, '_ticker', "TMF")
        _write_mts_state(has_position=False, action="CLOSE", reason=reason or "trail_exit", ticker=_ticker)
        self._has_position = False
        self._lifecycle = "FLAT"
        self._last_exit_ts = datetime.now()  # 2026-05-27 Gemini CLI: Enforce re-entry cooldown
        self._entry_ts = None
        self._near_entry = 0.0
        self._far_entry = 0.0
        self._near_side = None
        self._far_side = None
        self._entry_spread_z = 0.0
        self._released_leg = None
        self._release_ts = None
        self._peak = 0.0
        self._nadir = 0.0
        self._side = None
        # 2026-05-27 Gemini CLI: Watchdog metrics (P2)
        self._exit_start_time = 0.0

    def cleanup(self) -> None:
        self._reset()
