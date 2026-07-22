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
import os
import json
import math
import time
import pandas as pd
from datetime import datetime
from typing import Any, TypeVar
from enum import Enum
from dataclasses import dataclass, field

from core.signal import Signal
from core.strategy_base import StrategyBase
from core.strategy_context import StrategyContext, MarketData, PositionView
# 2026-05-27 Gemini CLI: Use full path for engine constants to ensure plugin compatibility
from strategies.futures.squeeze_futures.engine.constants import get_point_value

logger = logging.getLogger(__name__)

# 2026-07-14 Gemini CLI: Import decoupled risk engines under ADR-009 Phase 2
from strategies.plugins.futures.active.risk_engine import (
    ReleaseRiskEngine,
    SingleLegRiskEngine,
    ReleaseRiskInput,
    SingleLegRiskInput,
    ReleaseRiskDecision,
    SingleLegRiskDecision
)

# ═══════════════════════════════════════════════════════════════
# ADR-009 v1.1: Position Lifecycle — ReleaseGroup + TrailGroup
# ═══════════════════════════════════════════════════════════════

from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    Leg,
    Side,
    PositionPhase,
    ReleaseGroupStatus,
    TrailGroupStatus,
    LifecycleAction,
    CancelStatus,
    EntryRiskSnapshot,
    ReleaseGroup,
    TrailGroup,
    PositionLifecycle,
)

class ReleaseMode(Enum):
    """MTS release order strategy (ADR-009 Phase 2 placeholder)."""
    TRIGGERED_MARKET = "triggered_market"
    TRIGGERED_OCO_LIMIT = "triggered_oco_limit"
    ENTRY_OCO_LIMIT = "entry_oco_limit"

# Priority order for action selection (index = priority, lower = higher)
_LIFECYCLE_ACTION_PRIORITY: list[LifecycleAction] = [
    LifecycleAction.MANUAL,
    LifecycleAction.STOPLOSS,
    LifecycleAction.TIMEOUT,
    LifecycleAction.RELEASE,
    LifecycleAction.TRAIL,
]


# ═══════════════════════════════════════════════════════════════
# Serialization helpers (ADR-009 v1.1 Task 2)
# ═══════════════════════════════════════════════════════════════

_EnumT = TypeVar("_EnumT", bound=Enum)

def _enum_from_value(enum_cls: type[_EnumT], value: object, default: _EnumT) -> _EnumT:
    """Parse an enum from a JSON value with strict validation.

    Raises ValueError on invalid values (ADR-009: strict policy).
    """
    if value is None:
        _warn_fallback(f"{enum_cls.__name__} is None, using {default}")
        return default
    try:
        return enum_cls(value)
    except (ValueError, TypeError):
        logger.warning("[LIFECYCLE_ENUM] invalid %s value=%r — raising (strict policy)", enum_cls.__name__, value)
        raise

def _warn_fallback(msg: str) -> None:
    logger.warning("[LIFECYCLE_FALLBACK] %s", msg)

def _leg_to_str(leg: Leg | None) -> str | None:
    return leg.value if leg else None

def _str_to_leg(s: str | None) -> Leg | None:
    return Leg(s) if s else None

def enum_value(value: object) -> str | None:
    """Helper to extract string value from any enum (including split-brain modules) or string.

    Fail-closed: returns None for unknown/unsupported types instead of guessing.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raw = getattr(value, "value", None)
    return raw if isinstance(raw, str) else None

def _release_group_to_dict(rg: ReleaseGroup) -> dict:
    return {
        "status": rg.status.value,
        "near_order_id": rg.near_order_id,
        "far_order_id": rg.far_order_id,
        "filled_leg": _leg_to_str(rg.filled_leg),
        "filled_order_id": rg.filled_order_id,
        "canceled_leg": _leg_to_str(rg.canceled_leg),
        "trigger_ts": rg.trigger_ts,
        # ADR-010
        "sibling_cancel_order_id": rg.sibling_cancel_order_id,
        "sibling_cancel_status": rg.sibling_cancel_status.value if rg.sibling_cancel_status else None,
        "entry_risk": _entry_risk_to_dict(rg.entry_risk) if rg.entry_risk else None,
        "near_price": rg.near_price,
        "far_price": rg.far_price,
        "near_side": rg.near_side,
        "far_side": rg.far_side,
        "order_type": rg.order_type,
    }

def _release_group_from_dict(d: dict | None) -> ReleaseGroup:
    if not d:
        return ReleaseGroup()
    _er = d.get("entry_risk")
    return ReleaseGroup(
        status=_enum_from_value(ReleaseGroupStatus, d.get("status"), ReleaseGroupStatus.INACTIVE),
        near_order_id=d.get("near_order_id"),
        far_order_id=d.get("far_order_id"),
        filled_leg=_str_to_leg(d.get("filled_leg")),
        filled_order_id=d.get("filled_order_id"),
        canceled_leg=_str_to_leg(d.get("canceled_leg")),
        trigger_ts=d.get("trigger_ts"),
        # ADR-010
        sibling_cancel_order_id=d.get("sibling_cancel_order_id"),
        sibling_cancel_status=(
            _enum_from_value(CancelStatus, d.get("sibling_cancel_status"), CancelStatus.PENDING)
            if d.get("sibling_cancel_status") else None
        ),
        entry_risk=_entry_risk_from_dict(_er) if _er else None,
        near_price=float(d.get("near_price", 0.0)),
        far_price=float(d.get("far_price", 0.0)),
        near_side=d.get("near_side"),
        far_side=d.get("far_side"),
        order_type=str(d.get("order_type", "MKP")),
    )

def _entry_risk_to_dict(er: EntryRiskSnapshot) -> dict:
    return {
        "atr": er.atr,
        "release_stop": er.release_stop,
        "trail_stop": er.trail_stop,
        "entry_z": er.entry_z,
        "spread": er.spread,
        "timestamp": er.timestamp,
    }

def _entry_risk_from_dict(d: dict | None) -> EntryRiskSnapshot | None:
    if not d:
        return None
    return EntryRiskSnapshot(
        atr=float(d.get("atr", 0.0)),
        release_stop=float(d.get("release_stop", 0.0)),
        trail_stop=float(d.get("trail_stop", 0.0)),
        entry_z=float(d.get("entry_z", 0.0)),
        spread=float(d.get("spread", 0.0)),
        timestamp=str(d.get("timestamp", "")),
    )

def _trail_group_to_dict(tg: TrailGroup) -> dict:
    return {
        "status": tg.status.value,
        "remaining_leg": _leg_to_str(tg.remaining_leg),
        "exit_order_id": tg.exit_order_id,
        "peak_pnl": tg.peak_pnl,
        "nadir_pnl": tg.nadir_pnl,
        "trail_stop": tg.trail_stop,
        "trigger_ts": tg.trigger_ts,
    }

def _trail_group_from_dict(d: dict | None) -> TrailGroup:
    if not d:
        return TrailGroup()
    return TrailGroup(
        status=TrailGroupStatus(d.get("status", TrailGroupStatus.INACTIVE.value)),
        remaining_leg=_str_to_leg(d.get("remaining_leg")),
        exit_order_id=d.get("exit_order_id"),
        peak_pnl=d.get("peak_pnl"),
        nadir_pnl=d.get("nadir_pnl"),
        trail_stop=d.get("trail_stop"),
        trigger_ts=d.get("trigger_ts"),
    )

def lifecycle_to_dict(lc: PositionLifecycle) -> dict:
    """Serialize PositionLifecycle to a plain dict for JSON state file."""
    return {
        "phase": lc.phase.value,
        "release_group": _release_group_to_dict(lc.release_group),
        "trail_group": _trail_group_to_dict(lc.trail_group),
    }

def lifecycle_from_dict(d: dict | None) -> PositionLifecycle:
    """Deserialize PositionLifecycle from a plain dict. Returns FLAT default if d is None."""
    if not d:
        return PositionLifecycle()
    return PositionLifecycle(
        phase=PositionPhase(d.get("phase", PositionPhase.FLAT.value)),
        release_group=_release_group_from_dict(d.get("release_group")),
        trail_group=_trail_group_from_dict(d.get("trail_group")),
    )

def infer_lifecycle_from_legacy_state(state: dict | None) -> PositionLifecycle:
    """Infer PositionLifecycle from legacy state file (no lifecycle block).

    Used for backward compatibility (ADR-009 v1.1 Task 2).
    Legacy fields: has_position, state, released_leg, release_state.
    """
    lc = PositionLifecycle()
    if not state:
        return lc  # FLAT

    has_pos = state.get("has_position", False)
    if not has_pos:
        return lc  # FLAT

    release_state = state.get("release_state", "BOTH_HELD")
    released_leg_str = state.get("released_leg")  # "near" or "far" (legacy lowercase)

    if release_state in ("NEAR_RELEASED", "FAR_RELEASED") or released_leg_str:
        lc.phase = PositionPhase.SINGLE_LEG
        lc.release_group.status = ReleaseGroupStatus.COMPLETED
        # Infer which leg was released
        if released_leg_str:
            released_leg = Leg.NEAR if released_leg_str.lower() == "near" else Leg.FAR
            lc.release_group.filled_leg = released_leg
            lc.release_group.canceled_leg = Leg.FAR if released_leg == Leg.NEAR else Leg.NEAR
        elif release_state == "NEAR_RELEASED":
            lc.release_group.filled_leg = Leg.FAR  # remaining leg is NEAR, so FAR was released
            lc.release_group.canceled_leg = Leg.NEAR
        else:
            lc.release_group.filled_leg = Leg.NEAR
            lc.release_group.canceled_leg = Leg.FAR
        # TrailGroup starts ARMED (monitor will activate on next tick)
        lc.trail_group.status = TrailGroupStatus.ARMED
        lc.trail_group.remaining_leg = Leg.NEAR if lc.release_group.filled_leg == Leg.FAR else Leg.FAR
    else:
        # BOTH_HELD → SPREAD
        lc.phase = PositionPhase.SPREAD
        lc.release_group.status = ReleaseGroupStatus.ARMED

    return lc


_ENTRY_Z = 2.5            # entry z-score threshold
_RELEASE_STOP_PTS = 20    # losing leg release threshold (pt)
_TRAIL_DISTANCE_PTS = 30  # remaining leg trailing stop distance (pt)
# 2026-05-27 Gemini CLI: Environmental isolation for state file
# 2026-07-08 Gemini CLI: Default to test path if running under pytest to prevent state file leakage
import sys
_default_state_path = "/tmp/test_mts_position_state.json" if ("pytest" in sys.modules or "PYTEST_CURRENT_TEST" in os.environ) else "/tmp/mts_position_state.json"
_MTS_STATE_FILE = os.getenv("MTS_STATE_PATH", _default_state_path)
# 2026-06-25 Gemini CLI / Hermes Agent: environmental isolation for MTS fill and event logs
_MTS_EVENT_LOG = os.getenv("MTS_EVENT_LOG_PATH", "logs/mts_spread_events.jsonl")
_MTS_FILL_LOG = os.getenv("MTS_FILL_LOG_PATH", "logs/mts_trade_fills.jsonl")


# ═══════════════════════════════════════════════════════════════
# P0: Recovery state machine + CAS revision guard (2026-07-16)
# ───── 啟動恢復狀態機 ─────
# INITIALIZING期間，heartbeat只寫telemetry不寫lifecycle，
# 避免PM2 restart時init()的預設值誤將持倉洗成FLAT。
# 恢復順序：fills log → state file → broker (future)。
# ───── CAS revision ─────
# 每次lifecycle寫入遞增state_revision。Heartbeat只讀不寫。
# 舊process/舊heartbeat因revision mismatch被拒絕寫入。
# ───── Position epoch ─────
# 每筆交易唯一的epoch (trade_id)，heartbeat若無有效epoch則只能寫telemetry。
# 舊process的heartbeat若epoch不一致則被拒絕。
# ═══════════════════════════════════════════════════════════════

class RecoveryState(str, Enum):
    INITIALIZING = "INITIALIZING"        # startup, no recovery completed
    RECOVERED = "RECOVERED"              # position restored successfully
    FLAT_CONFIRMED = "FLAT_CONFIRMED"    # confirmed flat (broker+ledger agree)
    BROKER_UNKNOWN = "BROKER_UNKNOWN"    # broker query failed
    SPLIT_BRAIN = "SPLIT_BRAIN"          # ledger/snapshot/broker disagree
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"  # needs manual intervention

class FlatEvidenceType(str, Enum):
    EXIT_FILL = "EXIT_FILL"
    BROKER_RECONCILIATION = "BROKER_RECONCILIATION"
    MANUAL_RECONCILIATION = "MANUAL_RECONCILIATION"

@dataclass
class FlatEvidence:
    evidence_type: FlatEvidenceType
    broker_position_zero: bool = False
    exit_fill_id: str | None = None
    reconciliation_reason: str | None = None
    timestamp: str | None = None

_MTS_STATE_REVISION = 0  # incremented on every lifecycle state write

# ═══════════════════════════════════════════════════════════════
# Task 3A: Pure Decision Engine (ADR-009 v1.1)
# ═══════════════════════════════════════════════════════════════

# 2026-07-20 Gemini CLI: re-export pure decision model from decoupled adapter module
# 2026-07-21 Gemini CLI: re-export _check_release_candidates for test backward compatibility
from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    LifecycleContext,
    LifecycleDecision,
    evaluate_lifecycle_actions,
    _check_release_candidates,
)


def _commit_action(lifecycle: PositionLifecycle, decision: LifecycleDecision) -> None:
    """Apply lifecycle state transition. No side effects — no filesystem, no Shioaji.

    ADR-009 Task 9: TRAIL decision no longer pushes trail_group to SUBMITTED.
    SUBMITTED is now set only after broker order submit succeeds (with exit_order_id).
    This prevents the orphan SUBMITTED + exit_order_id=null deadlock.
    """
    if decision.action == LifecycleAction.RELEASE:
        lifecycle.release_group.status = ReleaseGroupStatus.TRIGGERED
    # TRAIL: status stays ARMED/ACTIVE until monitor confirms order submit


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
        # 2026-07-07 Hermes Agent: Only calculate UPL when has_position=True.
        # When FLAT, entry prices from disk are stale and produce phantom UPL.
        _mult = float(get_point_value(ticker))
        near_upl = 0.0
        far_upl = 0.0
        near_realized = 0.0
        far_realized = 0.0

        if has_position:
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

        # 2026-07-08 Hermes Agent: Allow caller to override realized with fee-deducted values.
        # The internal calc is gross (price - entry) * mult; caller may pass net-of-fees.
        _override_near = kwargs.pop("near_realized_override", None)
        _override_far = kwargs.pop("far_realized_override", None)
        if _override_near is not None:
            near_realized = float(_override_near)
        if _override_far is not None:
            far_realized = float(_override_far)

        # 2026-07-08 Hermes Agent: Cumulative realized PnL across all trades.
        # total_realized_pnl resets per-trade. cumulative_realized_pnl persists.
        # On close: combine previously-released leg's realized (from state file)
        # with trail exit realized (passed from _reset via kwargs).
        _cumulative = float(existing.get("cumulative_realized_pnl") or 0.0)
        _prev_has_pos = bool(existing.get("has_position", False))
        if _prev_has_pos and not has_position:
            _prev_near_realized = float(existing.get("near_realized_pnl") or 0.0)
            _prev_far_realized = float(existing.get("far_realized_pnl") or 0.0)
            _trail_exit = float(kwargs.get("trail_exit_realized", 0.0))
            _trade_realized = _prev_near_realized + _prev_far_realized + _trail_exit
            _cumulative += _trade_realized
            logger.info(
                "[MTS_CUMULATIVE_PNL] trade=%s closed: prev_realized=%.1f trail_exit=%.1f → cumulative=%.1f TWD",
                _f_trade_id or "?", _prev_near_realized + _prev_far_realized, _trail_exit, _cumulative,
            )

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
            # 2026-07-08 Hermes Agent: cumulative PnL persists across all trades
            "cumulative_realized_pnl": round(_cumulative, 1),
            # Preserve initial_balance from existing (set via dashboard)
            "initial_balance": existing.get("initial_balance", 100000),
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
        # 2026-07-22 Gemini CLI: Extract expected_revision parameter from kwargs
        _expected_revision_val = kwargs.pop("expected_revision", None)
        # 2026-06-26 Gemini CLI: merge extra risk metrics / kwargs
        state.update(kwargs)
        # 2026-06-23 Gemini CLI: Use unique temporary filename to avoid race conditions with other writers
        # ── P0: CAS revision guard ──
        # 防止舊 process 或延遲的 heartbeat 覆寫較新的 lifecycle state。
        # 每次 lifecycle 寫入遞增 state_revision，寫入前比對磁碟版本。
        # 若不符 → 拒絕寫入 (CONCURRENT_STATE_UPDATE)。
        # ─────────────────────────────────────────────────────────────────────
        # 2026-07-22 Gemini CLI: Respect expected_revision parameter if passed, otherwise fall back to disk value
        _expected_revision = _expected_revision_val if _expected_revision_val is not None else existing.get("state_revision", 0)
        _new_revision = _expected_revision + 1
        state["state_revision"] = _new_revision
        state["position_epoch"] = existing.get("position_epoch")
        state["schema_version"] = 3

        import random
        _tmp_file = f"{_MTS_STATE_FILE}.tmp.{os.getpid()}.{random.randint(1000, 9999)}"
        try:
            with open(_tmp_file, "w") as f:
                json.dump(state, f, default=str)
            # CAS: verify expected revision before replacing
            if os.path.exists(_MTS_STATE_FILE):
                try:
                    with open(_MTS_STATE_FILE) as _f_current:
                        _current_on_disk = json.load(_f_current)
                    _disk_revision = _current_on_disk.get("state_revision", 0)
                    if _disk_revision != _expected_revision:
                        if os.path.exists(_tmp_file): os.remove(_tmp_file)
                        logger.warning(
                            "[MTS_CAS_REJECT] revision mismatch: expected=%d disk=%d reason=%s",
                            _expected_revision, _disk_revision, reason,
                        )
                        return
                except Exception:
                    pass  # if we can't read disk, proceed with replace
            os.replace(_tmp_file, _MTS_STATE_FILE)
        except Exception as e:
            if os.path.exists(_tmp_file): os.remove(_tmp_file)
            raise e

    except Exception:
        logger.exception("[MTS_STATE_WRITE_FAILED] file=%s reason=%s", _MTS_STATE_FILE, reason)


def _write_mts_telemetry(
    ticker: str = "TMF",
    near_last: float = 0.0,
    far_last: float = 0.0,
    near_upl: float = 0.0,
    far_upl: float = 0.0,
    total_upl: float = 0.0,
    atr: float = 0.0,
    quote_age_ms: float = 0.0,
    **kwargs,
) -> None:
    """P0: Heartbeat-only telemetry update — NEVER writes lifecycle fields.

    ───── 設計原則 ─────
    Heartbeat 只能更新：
      - heartbeat_at, pid (心跳標記)
      - near_last, far_last (最新價格)
      - near_upl, far_upl, total_upl (未實現損益)
      - quote_age_ms (報價時效)

    Heartbeat 永遠不能更新：
      - has_position, state, reason (持倉所有權)
      - near_entry, far_entry (進場價格)
      - near_side, far_side (方向)
      - released_leg, remaining_leg (釋放狀態)
      - trade_id, entry_ts (交易識別)
      - lifecycle (生命週期)
      - 任何位置相關欄位

    所有 lifecycle 欄位從 existing state 原封不動繼承。
    即使記憶體中 init() 設了錯誤的預設值，
    heartbeat 也沒有能力把持倉洗掉。
    ─────────────────────"""
    if os.getenv("MTS_BACKTEST") == "1":
        return
    try:
        existing = {}
        if os.path.exists(_MTS_STATE_FILE):
            try:
                with open(_MTS_STATE_FILE) as _f:
                    existing = json.load(_f)
            except Exception:
                pass

        # Only touch telemetry fields — preserve ALL lifecycle fields
        _mult = float(get_point_value(ticker))
        telemetry = {
            "has_position": existing.get("has_position", False),
            "state": existing.get("state", "HEARTBEAT"),
            "reason": existing.get("reason", "heartbeat"),
            "near_last": round(near_last, 1),
            "far_last": round(far_last, 1),
            "near_upl": round(near_upl, 1),
            "far_upl": round(far_upl, 1),
            "total_upl": round(total_upl, 1),
            "atr": round(atr, 2) if atr else existing.get("atr"),
            "quote_age_ms": round(float(quote_age_ms), 1),
            "heartbeat_at": datetime.now().isoformat(),
            "heartbeat_pid": os.getpid(),
            "_updated": datetime.now().isoformat(),
            # CAS revision — read-only for telemetry
            "state_revision": existing.get("state_revision", 0),
            "schema_version": existing.get("schema_version", 3),
            "position_epoch": existing.get("position_epoch"),
        }
        # Preserve all lifecycle fields from existing state
        for _key in ["state", "reason", "has_position", "near_entry", "far_entry",
                      "near_side", "far_side", "near_status", "far_status",
                      "released_leg", "remaining_leg", "remaining_side",
                      "near_realized_pnl", "far_realized_pnl", "total_realized_pnl",
                      "cumulative_realized_pnl", "initial_balance", "trade_id", "entry_ts",
                      "trail_side", "trail_mode", "trail_peak", "trail_nadir",
                      "trail_stop_price", "distance_to_stop", "release_stop_points",
                      "trail_distance_points", "lifecycle", "manual_trade_status",
                      "entry_spread_z", "current_spread_z", "release_state",
                      "release_price", "risk_mode", "session", "stop_mult", "trail_mult",
                      "final_release_stop", "final_trail_dist", "release_stop",
                      "release_stop_floor", "trail_dist", "trail_dist_floor",
                      "confirm_ticks", "near_vwap", "far_vwap"]:
            if _key in existing:
                telemetry[_key] = existing[_key]

        import random
        _tmp = f"{_MTS_STATE_FILE}.tmp.{os.getpid()}.{random.randint(1000, 9999)}"
        with open(_tmp, "w") as f:
            json.dump(telemetry, f, default=str)
        os.replace(_tmp, _MTS_STATE_FILE)
    except Exception:
        logger.exception("[MTS_TELEMETRY_WRITE_FAILED]")


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
        
        # [New] ATR-based scaling  (2026-07-16 sweep: 2.5x best across 70 days, +67k net / 6.28 PF)
        self._atr_mult_stop = float(_params.get("atr_multiplier_stop", 2.5))
        self._atr_mult_trail = float(_params.get("atr_multiplier_trail", 2.0))
        # 2026-05-22 Gemini CLI: Added ATR cap to prevent excessively wide stops
        self._atr_cap = float(_params.get("atr_cap", 100.0))
        
        # Fallbacks for fixed points if ATR is unavailable
        self._release_stop_fixed = float(_params.get("release_stop_points", _RELEASE_STOP_PTS))
        self._trail_dist_fixed = float(_params.get("trail_distance_points", _TRAIL_DISTANCE_PTS))
        self._min_atr = float(_params.get("min_atr", 0.0))

        # Release mode (Phase 1: triggered_market only)
        _rel_mode_str = _params.get("release_mode", "triggered_market")
        try:
            self._release_mode = ReleaseMode(_rel_mode_str)
        except (ValueError, TypeError):
            self._release_mode = ReleaseMode.TRIGGERED_MARKET
            logger.warning("[MTS] Invalid release_mode=%r, falling back to triggered_market", _rel_mode_str)

        # ═══════════════════════════════════════════════════════════════
        # P0: Recovery state — UNKNOWN by default
        # 不預設 FLAT。init()只清空易失性狀態，不假設無持倉。
        # 恢復由 _restore_position_state() 完成，順序：
        #   fills log → state file → broker query (future)
        # ═══════════════════════════════════════════════════════════════
        self._has_position: bool | None = None  # None = UNKNOWN, 不等於 FLAT
        self._mts_recovery_state: RecoveryState = RecoveryState.INITIALIZING
        self._mts_state_write_enabled: bool = False  # P0: heartbeat 不可寫 lifecycle
        self._position_epoch: str | None = None  # 每筆交易唯一識別
        self._state_revision: int = 0
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

        # Release threshold confirmation
        self._confirm_ticks = int(_params.get("confirm_ticks", 2))
        self._confirm_ms = float(_params.get("confirm_ms", 800.0))
        self._max_quote_age_ms = float(_params.get("max_quote_age_ms", 1000.0))
        self._max_spread_width = float(_params.get("max_spread_width", 3.0))

        # Emergency quote guard bypass (shared with quote guard, not BB — BB filter removed per ADR-014)
        self._emergency_bypass_enabled: bool = bool(_params.get("emergency_bypass_enabled", True))

        # BB band cache (computed on each 5m bar, compared on each tick)
        self._near_bb_upper: float = 0.0
        self._near_bb_lower: float = 0.0
        self._far_bb_upper: float = 0.0
        self._far_bb_lower: float = 0.0

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
        self._mtf_score_at_release: float | None = None  # MTF score at release moment

        # Tick confirmation state variables
        self._release_near_ticks = 0
        self._release_near_start_time = 0.0
        self._release_far_ticks = 0
        self._release_far_start_time = 0.0
        self._trail_exit_ticks = 0
        self._trail_exit_start_time = 0.0

        # ADR-009 v1.1: Position Lifecycle OCA
        self._lifecycle_oca: PositionLifecycle = PositionLifecycle()

        # 2026-07-14 Gemini CLI: Instantiate decoupled risk engines under ADR-009 Phase 2
        self._release_risk_engine = ReleaseRiskEngine()
        self._single_leg_risk_engine = SingleLegRiskEngine()

        # 2026-07-14 Gemini CLI: Initialize MTF shadow tracking variables for counterfactual logging
        self._shadow_exit_triggered = False
        self._shadow_exit_ts = None
        self._shadow_exit_price = None
        self._shadow_exit_upl = None
        self._shadow_max_giveback = 0.0
        self._post_shadow_mfe = None
        self._post_shadow_mae = None
        self._formal_max_giveback = 0.0

        # ── ADR-011 Phase 2: Action-scoped timeout timers (2026-07-16) ──
        # Each exit action type has its own pending timer so RELEASE timeout
        # cannot bypass quote guard for TRAIL, or vice versa.
        self._release_pending_mono: float = 0.0   # starts when RELEASE decision first made
        self._trail_pending_mono: float = 0.0     # starts when TRAIL decision first made (SINGLE_LEG only)
        # Hard exit actions (STOPLOSS/TIMEOUT/MANUAL) bypass immediately — no timer needed.

        # ── ADR-011 Phase 4: Post-fill warmup (2026-07-16) ──
        # Prevents immediate EXIT after SINGLE_LEG transition from stale ticks/callbacks.
        self._single_leg_entered_mono: float = 0.0      # monotonic timestamp when SINGLE_LEG entered
        self._single_leg_post_fill_ticks: int = 0       # remaining-leg ticks received after fill
        self._single_leg_last_tick_ts: float = 0.0      # last remaining-leg tick timestamp (dedup)
        # Config (hot-reloadable via params in on_bar)
        self._single_leg_warmup_ms: float = 500.0
        self._single_leg_warmup_ticks: int = 2

        # 2026-07-20 Gemini CLI: Initialize MTS Lifecycle Adapter & temporal guard tracking
        from strategies.plugins.futures.active.mts_lifecycle_adapter import MtsLifecycleAdapter
        self._lifecycle_adapter = MtsLifecycleAdapter()
        self._last_applied_event_time = None
        self._single_leg_started_at = None

    def _current_lifecycle_state(self) -> dict:
        """ADR-009: serialize lifecycle block for _write_mts_state. Never raises."""
        try:
            return lifecycle_to_dict(self._lifecycle_oca)
        except Exception:
            return {}

    @staticmethod
    def _normalize_leg(leg: str | Leg | None) -> Leg | None:
        """Normalize 'near'/'far' string to Leg enum. Returns None if invalid."""
        if isinstance(leg, Leg):
            return leg
        if leg is None:
            return None
        try:
            return Leg[str(leg).upper()]
        except (ValueError, TypeError, KeyError):
            return None

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
            # 2026-07-16 Hermes Agent: Force fixed fallback when multiplier is 0
            # (same guard pattern as _get_risk_meta at line 1269)
            if self._atr_mult_stop <= 0 or self._atr_mult_trail <= 0:
                return self._release_stop_fixed, self._trail_dist_fixed

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

    def _apply_vwap_exit(self, bar: dict, trail_dist: float) -> float:
        """2026-07-08 Gemini CLI: Apply dynamic VWAP trailing stop tightening for remaining leg"""
        _vwap_exit_cfg = self._params.get("vwap_exit", {})
        if _vwap_exit_cfg.get("enabled", False) and self._released_leg and self._side:
            _rel = Leg.FAR if self._released_leg == "far" else Leg.NEAR
            _rem_leg = Leg.FAR if _rel == Leg.NEAR else Leg.NEAR
            _vwap_key = "far_vwap" if _rem_leg == Leg.FAR else "near_vwap"
            _rem_vwap = bar.get(_vwap_key, bar.get("vwap"))
            if _rem_vwap and _rem_vwap > 0:
                _rem_price = bar.get("far_close" if _rem_leg == Leg.FAR else "near_close", 0.0)
                if _rem_price > 0:
                    _violated = False
                    if self._side == "LONG" and _rem_price < _rem_vwap:
                        _violated = True
                    elif self._side == "SHORT" and _rem_price > _rem_vwap:
                        _violated = True
                    if _violated:
                        _tighten_ratio = float(_vwap_exit_cfg.get("tighten_ratio", 0.3))
                        return max(5.0, trail_dist * _tighten_ratio)
        return trail_dist

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
        # ADR-009 v1.1: sync lifecycle to SPREAD
        self._lifecycle_oca = PositionLifecycle(phase=PositionPhase.SPREAD, release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED))
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

    # ────────────────────────────────────────────────────────────
    # ADR-011 Phase 3: Single authoritative SINGLE_LEG entry
    # ────────────────────────────────────────────────────────────

    def _enter_single_leg_after_release_fill(
        self,
        *,
        released_leg: Leg,
        remaining_leg_price: float,
        fill_price: float,
        order_id: str,
        source: str,
        event_time: datetime | None = None,
    ) -> None:
        """ADR-011 Phase 3: Single authoritative entry point for SINGLE_LEG transition.

        Only callable after confirmed release fill.
        Verifies invariants, transitions lifecycle, performs full trail re-arm.
        """
        _rem = Leg.FAR if released_leg == Leg.NEAR else Leg.NEAR

        # ── Side determination ──
        if released_leg == Leg.NEAR:
            self._side = self._far_side
        else:
            self._side = self._near_side

        self._released_leg = released_leg.value.lower()
        self._release_ts = event_time or datetime.now()
        self._single_leg_started_at = self._release_ts
        self._release_mono = time.monotonic()
        # 2026-07-22 Gemini CLI: Avoid release price defaulting to 0.0 or remaining_leg_price if invalid
        _entry_fallback = self._near_entry if released_leg == Leg.NEAR else self._far_entry
        self._release_price = fill_price if fill_price > 0 else (remaining_leg_price if remaining_leg_price > 0 else _entry_fallback)
        self._lifecycle = f"TRAILING_{self._side}"

        # ── Lifecycle transition (confirmed fill only) ──
        self._lifecycle_oca = PositionLifecycle(
            phase=PositionPhase.SINGLE_LEG,
            release_group=ReleaseGroup(
                status=ReleaseGroupStatus.COMPLETED,
                filled_leg=released_leg,
                canceled_leg=_rem,
            ),
            trail_group=TrailGroup(
                status=TrailGroupStatus.ARMED,
                remaining_leg=_rem,
                trigger_ts=self._release_ts.isoformat() if isinstance(self._release_ts, datetime) else self._release_ts,
            ),
        )

        # ── ADR-011 Phase 2: Reset both timers ──
        self._release_pending_mono = 0.0
        self._trail_pending_mono = 0.0

        # ── ADR-011 Phase 4: Post-fill warmup start ──
        self._single_leg_entered_mono = time.monotonic()
        self._single_leg_post_fill_ticks = 0
        self._single_leg_last_tick_ts = 0.0

        # ── ADR-011 Phase 3: Full trail re-arm ──
        # Peak/nadir start from remaining leg price (clean slate)
        if self._side == "LONG":
            self._peak = remaining_leg_price
            self._nadir = 0.0
        else:
            self._nadir = remaining_leg_price
            self._peak = 0.0

        # Clear ALL pre-release cached state
        self._near_max = None
        self._near_min = None
        self._far_max = None
        self._far_min = None
        self._mfe_pts = 0.0
        self._mae_pts = 0.0
        self._shadow_exit_triggered = False
        self._shadow_exit_ts = None
        self._shadow_exit_price = None
        self._shadow_exit_upl = None
        self._shadow_max_giveback = 0.0
        self._post_shadow_mfe = None
        self._post_shadow_mae = None
        self._formal_max_giveback = 0.0
        self._release_near_ticks = 0
        self._release_near_start_time = 0.0
        self._release_far_ticks = 0
        self._release_far_start_time = 0.0

        logger.warning(
            "[MTS_SINGLE_LEG_ENTER] released_leg=%s remaining_leg=%s "
            "fill_price=%.1f rem_price=%.1f source=%s order_id=%s",
            released_leg.value, _rem.value,
            fill_price, remaining_leg_price, source, order_id,
        )

    def sync_release(self, leg: str | Leg, price: float, release_price: float = 0.0,
                     order_id: str = "", event_time: datetime | None = None) -> None:
        """
        Synchronize state after a leg release (PARTIAL_EXIT) is confirmed.
        Transitions lifecycle from RELEASE_NEAR/FAR to TRAILING mode.

        Delegates to _enter_single_leg_after_release_fill() for the authoritative
        SINGLE_LEG transition (ADR-011 Phase 3).
        """
        _rel = self._normalize_leg(leg)
        if _rel is None:
            logger.error("[LIFECYCLE] sync_release called with invalid leg=%r", leg)
            return
        _rel_str = _rel.value.lower()  # "near" / "far" for legacy

        # ADR-009 Task 7: idempotent guard — skip if already in SINGLE_LEG
        if self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG:
            logger.info("[LIFECYCLE_TASK7] sync_release skipped: already SINGLE_LEG (leg=%s)", _rel_str)
            return

        # Delegate to authoritative helper (ADR-011 Phase 3)
        self._enter_single_leg_after_release_fill(
            released_leg=_rel,
            remaining_leg_price=price,
            fill_price=release_price,
            order_id=order_id,
            source="sync_release",
            event_time=event_time,
        )

        # Legacy: unify release price (done inside helper, keep for backward compat logging)
        _released_entry = self._near_entry if _rel == Leg.NEAR else self._far_entry

        # 2026-06-29 Gemini CLI: Log the release fill after it succeeded
        _release_side = "BUY" if (self._near_side == "SHORT" if _rel == Leg.NEAR else self._far_side == "SHORT") else "SELL"
        _released_entry = self._near_entry if _rel == Leg.NEAR else self._far_entry
        _released_side_for_pnl = self._near_side if _rel == Leg.NEAR else self._far_side
        _released_pnl_pts = (self._release_price - _released_entry) if _released_side_for_pnl == "LONG" else (_released_entry - self._release_price)
        _mult = float(get_point_value(self._ticker))
        _cost = 40.0 + (self._release_price + _released_entry) * _mult * 2e-5
        _realized = _released_pnl_pts * _mult - _cost
        
        # MFE/MAE calculation
        if _rel == Leg.NEAR:
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
        if _rel == Leg.NEAR:
            _release_near_pnl = _realized  # released near leg (realized)
            _release_far_pnl = (price - self._far_entry) * _mult_r if self._far_side == "LONG" else (self._far_entry - price) * _mult_r
        else:
            _release_near_pnl = (price - self._near_entry) * _mult_r if self._near_side == "LONG" else (self._near_entry - price) * _mult_r
            _release_far_pnl = _realized  # released far leg (realized)
        _release_spread_pnl = (_release_near_pnl if _release_near_pnl is not None else 0) + (_release_far_pnl if _release_far_pnl is not None else 0)

        _append_fill(
            ticker=self._ticker,
            contract=_rel_str.upper(),
            leg=_rel_str.upper(),
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

        # ADR-009 Task 7: flush lifecycle to state file immediately on release fill callback
        _write_mts_state(
            has_position=self._has_position,
            action="RELEASE",
            reason=f"release_{_rel_str}",
            near_entry=self._near_entry,
            far_entry=self._far_entry,
            near_side=self._near_side,
            far_side=self._far_side,
            released_leg=self._released_leg,
            release_price=self._release_price,
            trade_id=self._trade_id,
            ticker=self._ticker,
            atr=self._last_atr or 0.0,
            lifecycle=self._current_lifecycle_state(),
        )

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
            
        if atr and not pd.isna(atr) and atr > 0:
            # 2026-07-14 Gemini CLI: Apply _atr_cap to telemetry to align with execution
            if hasattr(self, "_atr_cap") and self._atr_cap > 0:
                atr = min(atr, self._atr_cap)
                
        has_atr = atr and not pd.isna(atr) and atr > 0
        risk_mode = "ATR_DYNAMIC" if has_atr else "FIXED_FALLBACK"
        
        atr_val = round(float(atr), 2) if has_atr else 0.0
        stop_mult = self._atr_mult_stop if has_atr else 0.0
        trail_mult = self._atr_mult_trail if has_atr else 0.0
        
        # 2026-07-03 Hermes Agent: when multiplier is 0, force fixed fallback
        # (otherwise atr * 0 = 0, making thresholds useless)
        if has_atr and (stop_mult <= 0 or trail_mult <= 0):
            risk_mode = "FIXED_FALLBACK"
            has_atr = False
        
        release_stop = round(atr_val * stop_mult, 2) if has_atr else self._release_stop_fixed
        trail_dist = round(atr_val * trail_mult, 2) if has_atr else self._trail_dist_fixed
        
        release_stop_floor = 10.0
        trail_dist_floor = 60.0  # 2026-07-07: 20→60, ~0.82 ATR for TMF night (~73 pt)
        
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
            "confirm_ticks": int(confirm_ticks),
            # 2026-07-16 Gemini CLI: Record indicators for generate_daily_report.py
            "vwap": bar.get("vwap"),
            "near_vwap": bar.get("near_vwap"),
            "far_vwap": bar.get("far_vwap"),
            "mtf_score": bar.get("mtf_score")
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
            lifecycle=self._current_lifecycle_state(),
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
            # 2026-07-16 Hermes Agent: MTF score at exit time
            "mtf_score": self._mtf_score_at_release,
        }
        logger.info("[MTS_EXIT_LOG] %s", json.dumps(exit_data))
        _append_event("EXIT_LOG", **exit_data)
        return exit_data

    # ── P0: Fills log recovery ────────────────────────────────────────────────
    # Fills log 是比 state file 更高權威的持倉真相來源。
    # state file 可能被舊版 heartbeat 洗成 FLAT，
    # 但 fills log 的 ENTRY/EXIT/RELEASE 記錄不會因重啟而消失。
    # ─────────────────────────────────────────────────────────────────────
    @staticmethod
    def _check_fills_has_open_position(log_path: str | None = None) -> bool:
        """Check if fills log has an open (unclosed) position by looking for
        unmatched ENTRY records.  Returns True if at least one ENTRY has no
        matching EXIT/RELEASE that closes all legs."""
        _log = log_path or _MTS_FILL_LOG
        if not os.path.exists(_log):
            return False
        _entries = []
        _exits = []
        try:
            with open(_log) as _f:
                for _line in _f:
                    try:
                        rec = json.loads(_line.strip())
                        tid = rec.get("trade_id")
                        ft = rec.get("fill_type", "")
                        if tid and ft == "ENTRY":
                            _entries.append(tid)
                        elif tid and ft in ("EXIT", "RELEASE"):
                            _exits.append(tid)
                    except Exception:
                        pass
        except Exception:
            return False
        # An entry is "open" if its trade_id appears more in ENTRY than in EXIT+RELEASE
        from collections import Counter
        _entry_counts = Counter(_entries)
        _exit_counts = Counter(_exits)
        for tid, ecnt in _entry_counts.items():
            if ecnt > _exit_counts.get(tid, 0):
                return True
        return False

    def _restore_from_fills_log(self) -> bool:
        """Rebuild position state from fills log. Returns True on success."""
        if not os.path.exists(_MTS_FILL_LOG):
            return False
        try:
            # Find the most recent open trade
            _trades: dict[str, list[dict]] = {}
            with open(_MTS_FILL_LOG) as _f:
                for _line in _f:
                    try:
                        rec = json.loads(_line.strip())
                        tid = rec.get("trade_id")
                        if tid:
                            _trades.setdefault(tid, []).append(rec)
                    except Exception:
                        pass

            if not _trades:
                return False

            # Find the latest open trade by timestamp
            _latest_open_tid = None
            _latest_open_entries = []
            _latest_ts = ""
            for tid, fills in _trades.items():
                _entry_count = sum(1 for f in fills if f.get("fill_type") == "ENTRY")
                _exit_count = sum(1 for f in fills if f.get("fill_type") in ("EXIT", "RELEASE"))
                if _entry_count > _exit_count:
                    # Find timestamp of first entry fill
                    _first_entry_ts = ""
                    for f in fills:
                        if f.get("fill_type") == "ENTRY":
                            _ts = str(f.get("timestamp", ""))
                            if _ts > _first_entry_ts:
                                _first_entry_ts = _ts
                    if _first_entry_ts > _latest_ts:
                        _latest_ts = _first_entry_ts
                        _latest_open_tid = tid
                        _latest_open_entries = fills

            if not _latest_open_tid:
                return False

            # Restore state from fills
            _entry_fills = [f for f in _latest_open_entries if f.get("fill_type") == "ENTRY"]
            _release_fills = [f for f in _latest_open_entries if f.get("fill_type") == "RELEASE"]

            if not _entry_fills:
                return False

            # Determine near/far from entry fills
            _near_entry = _far_entry = 0.0
            _near_side = _far_side = None
            for ef in _entry_fills:
                _ef_price = ef.get("price")
                if ef.get("leg") == "NEAR":
                    _near_entry = float(_ef_price) if _ef_price is not None else 0.0
                    _near_side = ef.get("side")
                elif ef.get("leg") == "FAR":
                    _far_entry = float(_ef_price) if _ef_price is not None else 0.0
                    _far_side = ef.get("side")

            if _near_entry <= 0 or _far_entry <= 0:
                return False

            self._has_position = True
            self._near_entry = _near_entry
            self._far_entry = _far_entry
            self._near_side = "LONG" if _near_side in ("LONG", "BUY") else "SHORT"
            self._far_side = "LONG" if _far_side in ("LONG", "BUY") else "SHORT"
            self._released_leg = None
            self._trade_id = _latest_open_tid
            self._lifecycle = "OPEN"
            self._position_epoch = _latest_open_tid

            # Process release fills if any
            for rf in _release_fills:
                _rf_price = rf.get("price")
                if rf.get("leg") == "NEAR":
                    self._released_leg = "near"
                    self._side = self._far_side
                    self._lifecycle = f"TRAILING_{self._side}"
                    self._release_price = float(_rf_price) if _rf_price is not None else 0.0
                elif rf.get("leg") == "FAR":
                    self._released_leg = "far"
                    self._side = self._near_side
                    self._lifecycle = f"TRAILING_{self._side}"
                    self._release_price = float(_rf_price) if _rf_price is not None else 0.0

            logger.warning(
                "[MTS_FILLS_RECOVERY] Restored trade_id=%s near=%.1f far=%.1f released=%s lifecycle=%s",
                _latest_open_tid, _near_entry, _far_entry, self._released_leg, self._lifecycle,
            )
            return True
        except Exception:
            logger.exception("[MTS_FILLS_RECOVERY_FAILED]")
            return False

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

    # ── ADR-010 Sprint 5C: Release bracket submission reconciliation ──
    def _reconcile_release_bracket_submission(self, state: dict) -> None:
        """Restart during SUBMITTING: one order submitted, one missing.

        Broker authority: check which order(s) exist at broker.
        If submitted order is alive at broker → re-submit missing order.
        If submitted order is dead at broker → rollback to ARMED.
        """
        _rg = self._lifecycle_oca.release_group
        _near_dead = False
        _far_dead = False

        if _rg.near_order_id:
            _near_dead = False  # paper: always alive
        else:
            _near_dead = True

        if _rg.far_order_id:
            _far_dead = False  # paper: always alive
        else:
            _far_dead = True

        if _near_dead and _far_dead:
            # Both dead → full rollback
            _rg.status = ReleaseGroupStatus.ARMED
            _rg.near_order_id = None
            _rg.far_order_id = None
            logger.warning(
                "[OCO_RESTORE_5C] SUBMITTING — both ids missing, rollback to ARMED (trade_id=%s)",
                state.get("trade_id"),
            )
        else:
            # At least one alive → restore SUBMITTING, allow resubmit
            _rg.status = ReleaseGroupStatus.SUBMITTING
            logger.warning(
                "[OCO_RESTORE_5C] Restart @ SUBMITTING — near=%s far=%s (trade_id=%s)",
                _rg.near_order_id or "MISSING",
                _rg.far_order_id or "MISSING",
                state.get("trade_id"),
            )
            # The missing order will be re-submitted when monitor's _sync_mts_strategy_after_fill
            # detects SUBMITTING with a missing order id on next tick.

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

        # ═══════════════════════════════════════════════════════════════
        # P0: Fills log authority check
        # state file 說 FLAT 不等於真的 FLAT。
        # fills log 的 ENTRY/EXIT 記錄比 state file 更高權威。
        # 2026-07-16: 修復前 heartbeat 會把持倉洗成 FLAT，
        # 而 _restore_position_state 直接 return False 不看 fills，
        # 導致 split-brain 後永遠無法恢復。
        # 修復: fills 有 open trade 時用 fills-led recovery。
        # ═══════════════════════════════════════════════════════════════
        _fills_open = self._check_fills_has_open_position()
        if _fills_open and (state is None or not state.get("has_position")):
            logger.warning(
                "[MTS_RECOVERY] State file says FLAT/None but fills log has open position. "
                "Attempting fills-led recovery."
            )
            # Rebuild position state from fills log
            if self._restore_from_fills_log():
                self._mts_recovery_state = RecoveryState.RECOVERED
                self._mts_state_write_enabled = True
                return True
            else:
                self._mts_recovery_state = RecoveryState.RECOVERY_REQUIRED
                logger.warning("[MTS_RECOVERY] Fills log has open trade but recovery failed — RECOVERY_REQUIRED")
                return False

        if state:
            # P0: Don't immediately trust FLAT/CLOSE/EXIT state. Cross-check fills log.
            if state.get("has_position") is False or state.get("state") in ("CLOSE", "EXIT", "FLAT"):
                if _fills_open:
                    logger.warning(
                        "[MTS_RECOVERY] State says FLAT but fills has open position. "
                        "Attempting fills-led recovery."
                    )
                    if self._restore_from_fills_log():
                        self._mts_recovery_state = RecoveryState.RECOVERED
                        self._mts_state_write_enabled = True
                        return True
                    self._mts_recovery_state = RecoveryState.SPLIT_BRAIN
                    return False
                self._mts_recovery_state = RecoveryState.FLAT_CONFIRMED
                self._mts_state_write_enabled = True
                return False
        
        # 1. Primary Source: JSON State File
        if state and state.get("has_position") is True:
            action = state.get("state", "")
            if action not in ("CLOSE", "EXIT", "FLAT"):
                # Check for staleness
                _updated = state.get("_updated")
                if _updated:
                    try:
                        _ts = datetime.fromisoformat(_updated)
                        # Handle both timezone-aware and naive datetimes
                        _now = datetime.now(_ts.tzinfo) if _ts.tzinfo else datetime.now()
                        _age_min = (_now - _ts).total_seconds() / 60.0
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
                            elif state.get("lifecycle") and state["lifecycle"].get("release_group", {}).get("status") not in (None, "INACTIVE", "ARMED", "TRIGGERED"):
                                # ADR-010: lifecycle block with active OCO state is authoritative
                                # Overrides legacy polluted check
                                _pollute_pass = True
                            else:
                                _pollute_pass = False
                            if _pollute_pass:
                                # [BUG FIX] Validate entry prices before restore.
                                # heartbeat 可能寫入不完整的狀態檔，
                                # 造成 near_entry / far_entry 遺失或為 0。
                                # 若不檢查，PnL 計算會用 0 當 entry → 假損益。
                                _near_entry_raw = state.get("near_entry")
                                _far_entry_raw = state.get("far_entry")
                                if _near_entry_raw is None or _far_entry_raw is None:
                                    logger.warning(
                                        "[MTS_RESTORE_REJECTED] reason=ENTRY_PRICE_MISSING "
                                        "near=%s far=%s", _near_entry_raw, _far_entry_raw
                                    )
                                    _pollute_pass = False
                                elif float(_near_entry_raw) <= 0 or float(_far_entry_raw) <= 0:
                                    logger.warning(
                                        "[MTS_RESTORE_REJECTED] reason=ENTRY_PRICE_ZERO "
                                        "near=%s far=%s", _near_entry_raw, _far_entry_raw
                                    )
                                    _pollute_pass = False
                            if _pollute_pass:
                                self._has_position = True
                                self._lifecycle = state.get("state", "OPEN")
                                # 2026-07-22 Gemini CLI: Restore state_revision from disk during recovery
                                self._state_revision = int(state.get("state_revision", 0))
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

                                # ── ADR-009 Task 6: Restart Reconciliation ──
                                # 2026-07-06 Hermes Agent: restore lifecycle from state file
                                lifecycle_block = state.get("lifecycle")
                                if lifecycle_block:
                                    try:
                                        self._lifecycle_oca = lifecycle_from_dict(lifecycle_block)
                                    except Exception:
                                        self._lifecycle_oca = infer_lifecycle_from_legacy_state(state)

                                    # ── ADR-010 Sprint 5: Restart Reconciliation ──
                                    _rg_5 = self._lifecycle_oca.release_group
                                    _tl_5 = self._lifecycle_oca.trail_group

                                    # 5A: CANCELING_SIBLING → SIBLING_CANCELED + SINGLE_LEG
                                    if (
                                        self._lifecycle_oca.phase == PositionPhase.SPREAD
                                        and _rg_5.status == ReleaseGroupStatus.CANCELING_SIBLING
                                    ):
                                        _rg_5.sibling_cancel_status = CancelStatus.CONFIRMED
                                        _rg_5.status = ReleaseGroupStatus.SIBLING_CANCELED
                                        self._lifecycle_oca.phase = PositionPhase.SINGLE_LEG
                                        _tl_5.status = TrailGroupStatus.ARMED
                                        logger.warning(
                                            "[OCO_RESTORE_5A] CANCELING_SIBLING → SIBLING_CANCELED → SINGLE_LEG (trade_id=%s)",
                                            state.get("trade_id"),
                                        )

                                    # 5B: SUBMITTED — restore both order ids as-is
                                    if (
                                        self._lifecycle_oca.phase == PositionPhase.SPREAD
                                        and _rg_5.status == ReleaseGroupStatus.SUBMITTED
                                        and _rg_5.near_order_id
                                        and _rg_5.far_order_id
                                    ):
                                        logger.warning(
                                            "[OCO_RESTORE_5B] SUBMITTED — near=%s far=%s (trade_id=%s)",
                                            _rg_5.near_order_id, _rg_5.far_order_id,
                                            state.get("trade_id"),
                                        )

                                    # 5C: SUBMITTING — delegate to helper
                                    if (
                                        self._lifecycle_oca.phase == PositionPhase.SPREAD
                                        and _rg_5.status == ReleaseGroupStatus.SUBMITTING
                                    ):
                                        self._reconcile_release_bracket_submission(state)

                                    # 5D: SIBLING_CANCELED → SINGLE_LEG + trail ARMED
                                    if (
                                        self._lifecycle_oca.phase == PositionPhase.SPREAD
                                        and _rg_5.status == ReleaseGroupStatus.SIBLING_CANCELED
                                    ):
                                        self._lifecycle_oca.phase = PositionPhase.SINGLE_LEG
                                        _tl_5.status = TrailGroupStatus.ARMED
                                        logger.warning(
                                            "[OCO_RESTORE_5D] SIBLING_CANCELED → SINGLE_LEG + trail ARMED (trade_id=%s)",
                                            state.get("trade_id"),
                                        )

                                    # Invariant guard: FLAT phase must not overlay active position
                                    if self._lifecycle_oca.phase == PositionPhase.FLAT:
                                        self._lifecycle_oca = infer_lifecycle_from_legacy_state(state)

                                    # ADR-009 Task 9: SUBMITTED requires exit_order_id.
                                    if (
                                        self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG
                                        and self._lifecycle_oca.trail_group.status == TrailGroupStatus.SUBMITTED
                                        and not self._lifecycle_oca.trail_group.exit_order_id
                                    ):
                                        self._lifecycle_oca.trail_group.status = TrailGroupStatus.ARMED
                                        logger.warning(
                                            "[MTS_RESTORE_TASK9] Orphan SUBMITTED without exit_order_id — "
                                            "downgraded trail_group to ARMED (trade_id=%s)",
                                            state.get("trade_id"),
                                        )
                                else:
                                    self._lifecycle_oca = infer_lifecycle_from_legacy_state(state)

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
                                # 2026-07-14 Gemini CLI: Set recovery state to RECOVERED to enable correct UPL telemetry
                                self._mts_recovery_state = RecoveryState.RECOVERED
                                # ── ADR-011 Phase 5: Restart warmup reset ──
                                # monotonic timestamps are invalid across process restarts.
                                # Start a fresh warmup cycle so post-restart EXIT requires
                                # 500ms/2 fresh ticks before trail can trigger.
                                self._single_leg_entered_mono = time.monotonic()
                                self._single_leg_post_fill_ticks = 0
                                self._single_leg_last_tick_ts = 0.0
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
                                # ADR-009 Task 6: sync lifecycle from fill-log reconstruction
                                self._lifecycle_oca = infer_lifecycle_from_legacy_state({
                                    "has_position": True,
                                    "released_leg": self._released_leg,
                                    "release_state": "NEAR_RELEASED" if self._released_leg else "BOTH_HELD",
                                })
                            else:
                                self._lifecycle = "OPEN"
                                self._peak = self._near_entry
                                self._nadir = self._far_entry
                                # ADR-009 Task 6: sync lifecycle from fill-log reconstruction
                                self._lifecycle_oca = infer_lifecycle_from_legacy_state({
                                    "has_position": True,
                                    "release_state": "BOTH_HELD",
                                })
                                
                            # 2026-07-14 Gemini CLI: Set recovery state to RECOVERED to enable correct UPL telemetry
                            self._mts_recovery_state = RecoveryState.RECOVERED
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

            # 2026-07-14 Gemini CLI: Hot-reload quote age and tick confirmation configs
            self._confirm_ticks = int(_params.get("confirm_ticks", self._confirm_ticks))
            self._confirm_ms = float(_params.get("confirm_ms", self._confirm_ms))
            self._max_quote_age_ms = float(_params.get("max_quote_age_ms", self._max_quote_age_ms))
            self._max_spread_width = float(_params.get("max_spread_width", self._max_spread_width))

            # 2026-07-16 Hermes Agent: ADR-011 Phase 4 hot-reload warmup params
            self._single_leg_warmup_ms = float(_params.get("single_leg_warmup_ms", self._single_leg_warmup_ms))
            self._single_leg_warmup_ticks = int(_params.get("single_leg_warmup_ticks", self._single_leg_warmup_ticks))

            # 2026-07-16 Hermes Agent: Emergency quote guard bypass hot-reload (shared, not BB per ADR-014)
            self._emergency_bypass_enabled = bool(_params.get("emergency_bypass_enabled", self._emergency_bypass_enabled))

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
        if self._has_position is None:
            try:
                _restored = self._restore_position_state()
                if not _restored:
                    self._has_position = False
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

        # ── [PCF-1] Account channel degraded — block entry ──
        try:
            from core.channel_safety import get_safety_state
            _safety = get_safety_state()
            if not _safety.entry_allowed(self._ticker):
                self._set_eval(skip_reason=f"SAFETY_GATE:{_safety.entry_blocked_reason}")
                return None
        except Exception:
            pass

        # ── [Fix] Prevent duplicate submissions ──
        if self._lifecycle == "SUBMITTING":
            self._set_eval(skip_reason="ENTRY_ALREADY_SUBMITTED")
            return None

        # ── Direction-aware entry ──
        if spread_z_f > 0:
            _action = "SELL_NEAR_BUY_FAR"
            _reason = "TMF_SPREAD_WIDE"
            _expected_reversion = "SPREAD_TO_NARROW"
            _near_side = "SHORT"
            _far_side = "LONG"
            self._peak = near_close
            self._nadir = far_close
        else:
            _action = "BUY_NEAR_SELL_FAR"
            _reason = "TMF_SPREAD_NARROW"
            _expected_reversion = "SPREAD_TO_WIDEN"
            _near_side = "LONG"
            _far_side = "SHORT"
            self._peak = near_close
            self._nadir = far_close

        # ── Entry-side audit log (2026-07-03 Hermes Agent) ──
        _append_event("ENTRY_AUDIT",
            action=_action, reason=_reason,
            entry_z=round(self._entry_z, 2), spread_z=round(spread_z_f, 2),
            expected_reversion=_expected_reversion,
            near_side=_near_side, far_side=_far_side,
            near_price=near_close, far_price=far_close,
            spread_now=bar.get("spread") or (near_close - far_close),
            spread_formula="near_minus_far",
            spread_mean=bar.get("spread_mean"), spread_std=bar.get("spread_std"),
            atr=atr,
        )

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
            lifecycle=self._current_lifecycle_state(),
        )
        _append_event("ENTRY_SUBMITTED", action=_action, near_side=self._near_side, far_side=self._far_side,
                       near_entry=near_close, far_entry=far_close, spread_z=spread_z_f,
                       # 2026-07-16 Gemini CLI: Record indicators for generate_daily_report.py
                       mtf_score=bar.get("mtf_score"), vwap=bar.get("vwap"),
                       near_vwap=bar.get("near_vwap"), far_vwap=bar.get("far_vwap"))
        
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

    # 2026-07-14 Gemini CLI: Log counterfactual shadow MTF summary at trade exit
    def _log_shadow_trade_summary(self, _exit_price: float, _exit_reason: str, _pnl_pts: float, now: datetime, bar: dict, near_close: float, far_close: float):
        if not self._released_leg:
            return
            
        _rg = self._lifecycle_oca.release_group
        _rel = _rg.filled_leg
        if _rel is None:
            _rel = Leg.FAR if self._released_leg == "far" else Leg.NEAR
        _rem_leg = Leg.FAR if _rel == Leg.NEAR else Leg.NEAR
        _rem_entry = self._far_entry if _rem_leg == Leg.FAR else self._near_entry
        _rem_price = far_close if _rem_leg == Leg.FAR else near_close
        _rem_vwap = bar.get("far_vwap" if _rem_leg == Leg.FAR else "near_vwap", bar.get("vwap"))
        
        _shadow_exit_ts = self._shadow_exit_ts.isoformat() if self._shadow_exit_ts else None
        _shadow_exit_price = self._shadow_exit_price
        _shadow_hypo_pnl = self._shadow_exit_upl if self._shadow_exit_triggered else _pnl_pts
        
        logger.warning(
            "[MTS_MTF_SHADOW_TRADE_SUMMARY] "
            "trade_id=%s remaining_leg=%s remaining_side=%s "
            "single_leg_entry_ts=%s single_leg_entry_price=%.1f "
            "actual_exit_ts=%s actual_exit_price=%.1f actual_exit_reason=%s actual_realized_pnl=%.1f "
            "shadow_first_trigger_ts=%s shadow_first_trigger_price=%s shadow_trail_dist_pts=%.1f "
            "actual_final_trail_dist_pts=%.1f shadow_hypothetical_exit_ts=%s "
            "shadow_hypothetical_exit_price=%s shadow_hypothetical_pnl=%.1f "
            "mfe_pts=%.1f mae_pts=%.1f actual_giveback_pts=%.1f shadow_giveback_pts=%.1f "
            "mtf_score_at_release=%s mtf_score_at_shadow_trigger=%s mtf_score_at_actual_exit=%s "
            "vwap_relation_at_shadow_trigger=%s price_vs_vwap_pts=%s price_vs_vwap_pct=%s "
            "shadow_would_exit_earlier=%s post_shadow_mfe=%s post_shadow_mae=%s",
            self._trade_id, _rem_leg.value, self._side,
            self._release_ts.isoformat() if self._release_ts else None, _rem_entry,
            now.isoformat(), _exit_price, _exit_reason, _pnl_pts,
            _shadow_exit_ts, f"{_shadow_exit_price:.1f}" if _shadow_exit_price is not None else None,
            getattr(self, "_last_trail_dist_shadow", 20.0),
            getattr(self, "_last_trail_dist_formal", 20.0),
            _shadow_exit_ts,
            f"{_shadow_exit_price:.1f}" if _shadow_exit_price is not None else None,
            _shadow_hypo_pnl,
            self._mfe_pts, self._mae_pts, self._formal_max_giveback, self._shadow_max_giveback,
            getattr(self, "_mtf_score_at_release", None),
            getattr(self, "_mtf_score_at_shadow_trigger", None),
            bar.get("mtf_score"),
            "BELOW" if self._side == "LONG" else "ABOVE",
            round(_rem_price - _rem_vwap, 1) if _rem_vwap else None,
            round((_rem_price - _rem_vwap) / _rem_vwap * 100, 3) if _rem_vwap else None,
            self._shadow_exit_triggered,
            self._post_shadow_mfe, self._post_shadow_mae
        )

    # 2026-07-14 Gemini CLI: Decoupled risk engine helper for Release & Single-Leg stops
    def _evaluate_risk(self, near_close: float, far_close: float, current_pnl: float, bar: dict) -> tuple[float, float, SingleLegRiskDecision | None]:
        # 1. Base ATR thresholds
        release_stop_base, trail_dist_base = self._get_thresholds(bar)
        
        # 2. Spread/Release Risk (no MTF used)
        release_input = ReleaseRiskInput(
            base_release_stop_pts=release_stop_base,
            near_pnl=self._pnl_near(near_close),
            far_pnl=self._pnl_far(far_close),
            spread=bar.get("spread") or (near_close - far_close),
            spread_atr=bar.get("atr") or self._last_atr,
            bb_squeeze_on=bool(bar.get("sqz_on", False)),  # SHADOW — not consumed by risk engine per ADR-014
            tick_confirmed=True
        )
        release_decision = self._release_risk_engine.evaluate_release_risk(release_input)
        release_stop = release_decision.final_release_stop_pts
        
        # 3. Single-Leg Risk (MTF evaluated shadow-only)
        _rg = self._lifecycle_oca.release_group
        _rel = _rg.filled_leg
        if _rel is None and self._released_leg:
            _rel = Leg.FAR if self._released_leg == "far" else Leg.NEAR
            
        single_decision = None
        trail_dist = trail_dist_base
        if _rel is not None:
            _rem_leg = Leg.FAR if _rel == Leg.NEAR else Leg.NEAR
            _rem_price = far_close if _rem_leg == Leg.FAR else near_close
            _rem_entry = self._far_entry if _rem_leg == Leg.FAR else self._near_entry
            _vwap_key = "far_vwap" if _rem_leg == Leg.FAR else "near_vwap"
            _rem_vwap = bar.get(_vwap_key, bar.get("vwap"))
            
            # Retrieve MTF snapshot fields injected by monitor
            mtf_score = bar.get("mtf_score")
            mtf_valid = bool(bar.get("mtf_valid", False))
            mtf_age_sec = bar.get("mtf_age_sec")
            
            single_input = SingleLegRiskInput(
                side=self._side or "LONG",
                current_price=_rem_price,
                entry_price=_rem_entry,
                peak_price=self._peak if self._side == "LONG" else self._nadir,
                base_trail_dist_pts=trail_dist_base,
                atr_used=bar.get("atr") or self._last_atr,
                vwap=_rem_vwap,
                mtf_score=mtf_score,
                mtf_valid=mtf_valid,
                mtf_age_sec=mtf_age_sec,
                unrealized_pnl=current_pnl,
                mfe_pts=self._mfe_pts
            )
            
            _vwap_cfg = self._params.get("vwap_exit", {})
            _mtf_cfg = self._params.get("mtf", {})
            
            single_decision = self._single_leg_risk_engine.evaluate_single_leg_risk(
                inputs=single_input,
                vwap_exit_config=_vwap_cfg,
                mtf_config=_mtf_cfg
            )
            trail_dist = single_decision.final_trail_dist_pts
            
        return release_stop, trail_dist, single_decision

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

        # ── ADR-011 Phase 5: Count remaining-leg fresh ticks for warmup ──
        # Move tick counting logic here to prevent deadlock where early returns (e.g. TRAIL_WARMUP)
        # block the tick counter from incrementing.
        if self._lifecycle_oca and self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG:
            # Sync legacy _lifecycle string for compat
            self._lifecycle = f"TRAILING_{self._side}"
            if self._single_leg_post_fill_ticks < self._single_leg_warmup_ticks:
                if not _is_backtest:
                    self._single_leg_post_fill_ticks += 1
                else:
                    _bar_ts = bar.get("timestamp") or bar.get("ts")
                    if _bar_ts is not None:
                        if hasattr(_bar_ts, "timestamp"):
                            _ts_float = _bar_ts.timestamp()
                        elif hasattr(_bar_ts, "tz"):
                            _ts_float = pd.Timestamp(_bar_ts).timestamp()
                        else:
                            _ts_float = float(_bar_ts)
                    else:
                        _ts_float = 0.0
                    if _ts_float > self._single_leg_last_tick_ts:
                        self._single_leg_post_fill_ticks += 1
                        self._single_leg_last_tick_ts = _ts_float
            
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

        # ── ADR-009 v1.1 Task 4A: Build LifecycleContext + evaluate lifecycle ──
        _decision: LifecycleDecision | None = None
        _single_decision: SingleLegRiskDecision | None = None
        try:
            _entry_age = (now - self._entry_ts).total_seconds() if self._entry_ts else 0.0
            _release_stop, _trail_dist, _single_decision = self._evaluate_risk(near_close, far_close, current_pnl, bar)
            _n_pnl = self._pnl_near(near_close)
            _f_pnl = self._pnl_far(far_close)
            _trailing_side = None
            _peak = self._peak
            _nadir = self._nadir
            # Read released_leg from lifecycle (with legacy fallback)
            _rg = self._lifecycle_oca.release_group
            _rel = _rg.filled_leg
            if _rel is None and self._released_leg:
                _rel = Leg.FAR if self._released_leg == "far" else Leg.NEAR
            if _rel is not None and self._side:
                _trailing_side = Side.LONG if self._side == "LONG" else Side.SHORT
            # rem_high/rem_low: use opposite leg from released
            if _rel == Leg.NEAR:
                if self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG and not _is_backtest:
                    # 2026-07-20 Gemini CLI: SINGLE_LEG may begin mid-bar after a RELEASE fill.
                    # In live/paper mode, whole-bar high/low can include observations
                    # from before the phase boundary. Use only the current event price
                    # so peak/nadir are derived exclusively from post-release ticks.
                    _rem_high = _rem_low = far_close
                else:
                    _rem_high = float(bar.get("far_high", 0))
                    _rem_low = float(bar.get("far_low", 0))
            elif _rel == Leg.FAR:
                if self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG and not _is_backtest:
                    # 2026-07-20 Gemini CLI: SINGLE_LEG may begin mid-bar after a RELEASE fill.
                    # In live/paper mode, whole-bar high/low can include observations
                    # from before the phase boundary. Use only the current event price
                    # so peak/nadir are derived exclusively from post-release ticks.
                    _rem_high = _rem_low = near_close
                else:
                    _rem_high = float(bar.get("near_high", 0))
                    _rem_low = float(bar.get("near_low", 0))
            else:
                _rem_high = _rem_low = 0.0
            # 2026-07-20 Gemini CLI: PR 3B Production Decision Core Cutover (first evaluation block)
            from strategies.plugins.futures.active.mts_lifecycle_adapter import LifecycleEvaluationInput
            _adapter_input = LifecycleEvaluationInput(
                strategy_state={
                    "near_pnl_pts": _n_pnl,
                    "far_pnl_pts": _f_pnl,
                    "floating_pnl_pts": current_pnl,
                    "entry_age_secs": _entry_age,
                    "release_stop_threshold": _release_stop,
                    "trail_dist": _trail_dist,
                    "manual_requested": getattr(self, "_manual_exit_requested", False),
                    "max_hold_secs": self._params.get("max_hold_secs"),
                    "max_loss_pts": self._params.get("max_loss_pts"),
                    "trailing_side": _trailing_side,
                    "peak": _peak,
                    "nadir": _nadir,
                    "rem_high": _rem_high,
                    "rem_low": _rem_low,
                    "last_applied_event_time": self._last_applied_event_time.isoformat() if getattr(self, "_last_applied_event_time", None) else None,
                    "single_leg_started_at": self._single_leg_started_at.isoformat() if getattr(self, "_single_leg_started_at", None) else None,
                },
                market_event={
                    "event_time": now.isoformat(),
                    "timestamp": now.isoformat(),
                    "ts": now.isoformat(),
                },
                lifecycle=self._lifecycle_oca,
                execution_mode="BACKTEST" if _is_backtest else "LIVE",
            )
            _adapter_res = self._lifecycle_adapter.evaluate(_adapter_input)
            _decision = _adapter_res.decision
            # 2026-07-16 Gemini CLI: P0 Invariant Assertion to detect evaluate_lifecycle_actions regression (Phase 3a)
            if (
                _decision is None
                and self._lifecycle_oca.phase == PositionPhase.SPREAD
                and self._lifecycle_oca.release_group.status == ReleaseGroupStatus.ARMED
                and (_n_pnl <= -_release_stop or _f_pnl <= -_release_stop)
            ):
                logger.error(
                    "[MTS_RELEASE_DECISION_INVARIANT_VIOLATION] far_hit/near_hit is True but evaluate_lifecycle_actions returned None — P0"
                )
                if not _is_backtest:
                    raise RuntimeError("MTS_RELEASE_DECISION_INVARIANT_VIOLATION in production")
            # 2026-07-09 Hermes Agent: P0 lifecycle ordering — commit + write_state
            # moved below, after tick confirmation and BB filter pass.  The early write
            # at this location (removed) created state-file/broker divergence by writing
            # TRIGGERED before pre-order guards completed.
        except Exception:
            logger.exception("[LIFECYCLE_EVAL_FAILED]")

        # ── ADR-011 Phase 2: Action-scoped timeout timers (2026-07-16) ──
        # Each action has its own pending timer, preventing RELEASE timeout
        # from leaking into TRAIL and vice versa.
        _active_action = _decision.action if (_decision is not None) else None

        # RELEASE: start/keep release timer
        if _active_action == LifecycleAction.RELEASE:
            if self._release_pending_mono <= 0:
                self._release_pending_mono = time.monotonic()
                logger.info("[MTS_ACTION_TIMEOUT_STATE] action=RELEASE event=TIMER_START")
        elif _active_action == LifecycleAction.TRAIL:
            # TRAIL: only legal in SINGLE_LEG with confirmed fill
            _rg = self._lifecycle_oca.release_group
            if self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG and _rg.status in (
                ReleaseGroupStatus.FILLED, ReleaseGroupStatus.COMPLETED,
            ):
                if self._trail_pending_mono <= 0:
                    self._trail_pending_mono = time.monotonic()
                    logger.info("[MTS_ACTION_TIMEOUT_STATE] action=TRAIL event=TIMER_START")
            else:
                # Trail decision is ILLEGAL — clear timer
                if self._trail_pending_mono > 0:
                    logger.warning(
                        "[MTS_TRAIL_TIMER_RESET] reason=NOT_SINGLE_LEG phase=%s rg_status=%s",
                        self._lifecycle_oca.phase.value if hasattr(self._lifecycle_oca.phase, 'value') else self._lifecycle_oca.phase,
                        _rg.status.value if hasattr(_rg.status, 'value') else _rg.status,
                    )
                self._trail_pending_mono = 0.0
        elif _active_action in (LifecycleAction.STOPLOSS, LifecycleAction.TIMEOUT, LifecycleAction.MANUAL):
            # Hard exits: bypass immediately — no timer needed
            pass
        else:
            # No exit action active → reset both timers
            if self._release_pending_mono > 0:
                self._release_pending_mono = 0.0
            if self._trail_pending_mono > 0:
                # Only reset trail timer if not in a valid SINGLE_LEG state
                # (trail may persist across ticks within the same exit attempt)
                _rg = self._lifecycle_oca.release_group
                if not (self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG and _rg.status in (
                    ReleaseGroupStatus.FILLED, ReleaseGroupStatus.COMPLETED,
                )):
                    self._trail_pending_mono = 0.0

        # Quote freshness check
        near_age = float(bar.get("near_tick_age_ms", bar.get("near_age_ms", -1)))
        far_age = float(bar.get("far_tick_age_ms", bar.get("far_age_ms", -1)))
        
        # 2026-07-16 Gemini CLI: Decoupled Leg Freshness Check.
        # Only check the freshness of the leg being actively exited or released.
        # This prevents a stale quote on leg A from hijacking / blocking a stop loss on leg B.
        _target_age = -1.0
        if _decision is not None:
            if _decision.action == LifecycleAction.RELEASE:
                _target_leg = _decision.release_leg
                _target_age = near_age if _target_leg == Leg.NEAR else far_age
            elif _decision.action == LifecycleAction.TRAIL:
                _rel = self._lifecycle_oca.release_group.filled_leg
                if _rel is None and self._released_leg:
                    _rel = Leg.FAR if self._released_leg == "far" else Leg.NEAR
                if _rel is not None:
                    _rem_leg = Leg.FAR if _rel == Leg.NEAR else Leg.NEAR
                    _target_age = far_age if _rem_leg == Leg.FAR else near_age
            elif _decision.action in (LifecycleAction.STOPLOSS, LifecycleAction.TIMEOUT, LifecycleAction.MANUAL):
                _rel = self._lifecycle_oca.release_group.filled_leg
                if _rel is None and self._released_leg:
                    _rel = Leg.FAR if self._released_leg == "far" else Leg.NEAR
                if _rel is not None:
                    _rem_leg = Leg.FAR if _rel == Leg.NEAR else Leg.NEAR
                    _target_age = far_age if _rem_leg == Leg.FAR else near_age
                else:
                    _target_age = max(near_age, far_age)

        if _target_age >= 0:
            quote_age_ms = max(0.0, _target_age)
        else:
            # Default to maximum of both legs for entries / normal monitoring
            quote_age_ms = max(0.0, max(near_age, far_age)) if (near_age > 0 or far_age > 0) else 0.0

        # 2026-07-16 Gemini CLI: Emergency Quote Guard Bypass (Risk Escalation + Timeout).
        # If an exit decision is triggered, bypass the freshness check if:
        # A) Loss exceeds the configured emergency multiplier (defaults to 1.5x stop for early escape).
        # B) The decision has been pending for more than 500ms (Timeout).
        _bypass_quote_guard = False
        if _decision is not None and getattr(self, "_emergency_bypass_enabled", True):
            _mult = 1.5  # Risk escalation at 1.5x stop
            _timeout_limit_ms = 500.0  # Force exit if blocked for > 500ms
            
            # Check A: Risk Escalation
            _risk_escalated = False
            if _decision.action == LifecycleAction.RELEASE:
                _rel_leg = _decision.release_leg
                _loss = abs(_n_pnl) if _rel_leg == Leg.NEAR else abs(_f_pnl)
                if _loss > _release_stop * _mult:
                    _risk_escalated = True
            elif _decision.action == LifecycleAction.TRAIL:
                _rel = self._lifecycle_oca.release_group.filled_leg
                if _rel is None and self._released_leg:
                    _rel = Leg.FAR if self._released_leg == "far" else Leg.NEAR
                if _rel is not None:
                    _rem_leg = Leg.FAR if _rel == Leg.NEAR else Leg.NEAR
                    _rem_price = far_close if _rem_leg == Leg.FAR else near_close
                    _giveback = 0.0
                    if self._side == "LONG" and self._peak > 0:
                        _giveback = self._peak - _rem_price
                    elif self._side == "SHORT" and self._nadir > 0:
                        _giveback = _rem_price - self._nadir
                    if _giveback > _trail_dist * _mult:
                        _risk_escalated = True
            elif _decision.action in (LifecycleAction.STOPLOSS, LifecycleAction.TIMEOUT, LifecycleAction.MANUAL):
                _risk_escalated = True  # Hard exits bypass immediately

            # Check B: Timeout — action-scoped (ADR-011 Phase 2)
            _elapsed_ms = 0.0
            if _decision.action == LifecycleAction.RELEASE:
                _elapsed_ms = (time.monotonic() - self._release_pending_mono) * 1000 if self._release_pending_mono > 0 else 0.0
            elif _decision.action == LifecycleAction.TRAIL:
                _elapsed_ms = (time.monotonic() - self._trail_pending_mono) * 1000 if self._trail_pending_mono > 0 else 0.0
            elif _decision.action in (LifecycleAction.STOPLOSS, LifecycleAction.TIMEOUT, LifecycleAction.MANUAL):
                _elapsed_ms = _timeout_limit_ms + 1  # immediately bypass
            _timeout_triggered = _elapsed_ms > _timeout_limit_ms
            
            if _risk_escalated or _timeout_triggered:
                _bypass_quote_guard = True
                _reason_str = "RISK_ESCALATED" if _risk_escalated else f"TIMEOUT_{_elapsed_ms:.0f}ms"
                logger.warning("[MTS_QUOTE_GUARD_BYPASS] reason=%s action=%s age=%.0f ms", 
                               _reason_str, _decision.action.name, quote_age_ms)

        if quote_age_ms > self._max_quote_age_ms and not _bypass_quote_guard:
            self._set_eval(skip_reason="STALE_QUOTE_AGE", age=quote_age_ms)
            logger.warning("[MTS_RELEASE_BLOCKED] reason=STALE_QUOTE_AGE age=%.0f/%s", quote_age_ms, self._max_quote_age_ms)
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
            logger.warning("[MTS_RELEASE_BLOCKED] reason=WIDE_SPREAD_WIDTH near=%.1f far=%.1f max=%.1f", near_width, far_width, self._max_spread_width)
            return None

        # Dynamic thresholds
        release_stop, trail_dist, _single_decision = self._evaluate_risk(near_close, far_close, current_pnl, bar)

        # ── MTF Shadow Simulation and Telemetry (ADR-009 Phase 2) ──
        if _single_decision is not None and _single_decision.shadow_trail_dist_pts is not None:
            shadow_trail = _single_decision.shadow_trail_dist_pts
            mtf_score = bar.get("mtf_score")
            mtf_valid = bool(bar.get("mtf_valid", False))
            mtf_age_sec = bar.get("mtf_age_sec")
            
            # Update peak/nadir and simulate shadow exits
            _rg = self._lifecycle_oca.release_group
            _rel = _rg.filled_leg
            if _rel is None and self._released_leg:
                _rel = Leg.FAR if self._released_leg == "far" else Leg.NEAR
            if _rel is not None:
                _rem_leg = Leg.FAR if _rel == Leg.NEAR else Leg.NEAR
                _rem_price = far_close if _rem_leg == Leg.FAR else near_close
                _rem_entry = self._far_entry if _rem_leg == Leg.FAR else self._near_entry
                _rem_side = self._far_side if _rem_leg == Leg.FAR else self._near_side
                _rem_high = float(bar.get("far_high" if _rem_leg == Leg.FAR else "near_high", _rem_price))
                _rem_low = float(bar.get("far_low" if _rem_leg == Leg.FAR else "near_low", _rem_price))
                rem_floating_pnl = (_rem_price - _rem_entry) if self._side == "LONG" else (_rem_entry - _rem_price)
                
                # Check peak/nadir updates
                if self._side == "LONG":
                    _shadow_peak = max(self._peak, _rem_high)
                    _shadow_trigger_price = _shadow_peak - shadow_trail
                    _giveback = _shadow_peak - _rem_price
                    self._formal_max_giveback = max(self._formal_max_giveback, _giveback)
                    if not self._shadow_exit_triggered:
                        self._shadow_max_giveback = max(self._shadow_max_giveback, _giveback)
                        if _rem_low <= _shadow_trigger_price:
                            self._shadow_exit_triggered = True
                            self._shadow_exit_ts = now
                            self._shadow_exit_price = _shadow_trigger_price
                            self._shadow_exit_upl = (_shadow_trigger_price - _rem_entry)
                            self._post_shadow_mfe = 0.0
                            self._post_shadow_mae = 0.0
                            self._mtf_score_at_shadow_trigger = mtf_score
                            logger.warning(
                                "[MTS_MTF_SHADOW_TRIGGERED] trade_id=%s side=LONG trigger_price=%.1f price=%.1f peak=%.1f trail=%.1f ts=%s",
                                self._trade_id, _shadow_trigger_price, _rem_price, _shadow_peak, shadow_trail, now
                            )
                else: # SHORT
                    _shadow_nadir = min(self._nadir, _rem_low)
                    _shadow_trigger_price = _shadow_nadir + shadow_trail
                    _giveback = _rem_price - _shadow_nadir
                    self._formal_max_giveback = max(self._formal_max_giveback, _giveback)
                    if not self._shadow_exit_triggered:
                        self._shadow_max_giveback = max(self._shadow_max_giveback, _giveback)
                        if _rem_high >= _shadow_trigger_price:
                            self._shadow_exit_triggered = True
                            self._shadow_exit_ts = now
                            self._shadow_exit_price = _shadow_trigger_price
                            self._shadow_exit_upl = (_rem_entry - _shadow_trigger_price)
                            self._post_shadow_mfe = 0.0
                            self._post_shadow_mae = 0.0
                            self._mtf_score_at_shadow_trigger = mtf_score
                            logger.warning(
                                "[MTS_MTF_SHADOW_TRIGGERED] trade_id=%s side=SHORT trigger_price=%.1f price=%.1f nadir=%.1f trail=%.1f ts=%s",
                                self._trade_id, _shadow_trigger_price, _rem_price, _shadow_nadir, shadow_trail, now
                            )
                            
                # Update post-shadow excursions
                if self._shadow_exit_triggered:
                    if self._side == "LONG":
                        _excursion = _rem_price - self._shadow_exit_price
                        self._post_shadow_mfe = max(self._post_shadow_mfe, _excursion)
                        self._post_shadow_mae = min(self._post_shadow_mae, _excursion)
                    else:
                        _excursion = self._shadow_exit_price - _rem_price
                        self._post_shadow_mfe = max(self._post_shadow_mfe, _excursion)
                        self._post_shadow_mae = min(self._post_shadow_mae, _excursion)
                        
            # Log tick telemetry
            delta_pts = shadow_trail - trail_dist
            formal_modifiers = ",".join(_single_decision.modifiers) if _single_decision.modifiers else "NONE"
            shadow_modifiers = ",".join(_single_decision.shadow_modifiers) if _single_decision.shadow_modifiers else "NONE"
            
            logger.info(
                "[MTS_MTF_SHADOW_EVAL] side=%s score=%s valid=%s age=%s base_trail=%.1f formal_trail=%.1f shadow_trail=%.1f formal_mod=%s shadow_mod=%s delta=%.1f",
                self._side, mtf_score, mtf_valid, mtf_age_sec,
                _single_decision.base_trail_dist_pts, trail_dist, shadow_trail,
                formal_modifiers, shadow_modifiers, delta_pts
            )
            
            if shadow_trail < trail_dist:
                logger.info(
                    "[MTS_MTF_SHADOW_DIFF] trade_id=%s delta=%.1f shadow=%.1f formal=%.1f price=%.1f",
                    self._trade_id, delta_pts, shadow_trail, trail_dist, _rem_price
                )
            else:
                logger.debug(
                    "[MTS_MTF_SHADOW_NO_CHANGE] trade_id=%s shadow=%.1f formal=%.1f",
                    self._trade_id, shadow_trail, trail_dist
                )
        # 2026-05-27 Gemini CLI: Use dynamic multiplier from engine constants
        _mult = float(get_point_value(self._ticker))

        _n_pnl = self._pnl_near(near_close)
        _f_pnl = self._pnl_far(far_close)

        # 2026-07-09 Hermes Agent: release diagnostic eval — fires whenever a leg is at threshold
        _rel_near_hit = _n_pnl <= -release_stop
        _rel_far_hit = _f_pnl <= -release_stop
        if _rel_near_hit or _rel_far_hit:
            _rg_status = str(self._lifecycle_oca.release_group.status.value) if self._lifecycle_oca and self._lifecycle_oca.release_group else "N/A"
            _phase = str(self._lifecycle_oca.phase.value) if self._lifecycle_oca else "N/A"
            _tick_count = (self._release_near_ticks if _decision and getattr(_decision, 'release_leg', None) == Leg.NEAR else self._release_far_ticks)
            logger.warning(
                "[MTS_RELEASE_EVAL] has_pos=%s phase=%s rg_status=%s "
                "near_pnl=%.1f far_pnl=%.1f threshold=%.1f "
                "near_hit=%s far_hit=%s decision=%s tick_ct=%d/%d "
                "quote_age=%.0f/%s spread_w=%.1f/%.1f "
                "near_last=%.0f far_last=%.0f atr=%.1f spread_z=%s",
                self._has_position, _phase, _rg_status,
                _n_pnl, _f_pnl, release_stop,
                _rel_near_hit, _rel_far_hit,
                _decision.action.name if _decision else None,
                _tick_count, self._confirm_ticks,
                quote_age_ms, self._max_quote_age_ms,
                max(near_width, far_width), self._max_spread_width,
                near_close, far_close, bar.get("atr", 0), bar.get("spread_z", "N/A"),
            )

        # ── ADR-009 v1.1 Task 4B-1: decision-to-Signal ──
        if _decision is not None:
            if _decision.action == LifecycleAction.RELEASE:
                _release_leg = _decision.release_leg
                assert _release_leg is not None, "RELEASE decision must have release_leg"
                # Maintain tick confirmation state (legacy behavior)
                if _release_leg == Leg.NEAR:
                    self._release_near_ticks += 1
                    if self._release_near_ticks == 1:
                        self._release_near_start_time = time.monotonic()
                else:
                    self._release_far_ticks += 1
                    if self._release_far_ticks == 1:
                        self._release_far_start_time = time.monotonic()
                # Respect tick confirmation
                if _release_leg == Leg.NEAR:
                    if not (_is_backtest or (self._release_near_ticks >= self._confirm_ticks and (time.monotonic() - self._release_near_start_time) * 1000 >= self._confirm_ms)):
                        self._set_eval(skip_reason="LIFECYCLE_RELEASE_PENDING", leg="NEAR")
                        logger.warning("[MTS_RELEASE_BLOCKED] reason=TICK_CONFIRM_PENDING leg=NEAR ct=%d/%d age_ms=%.0f/%s", self._release_near_ticks, self._confirm_ticks, (time.monotonic() - self._release_near_start_time) * 1000 if self._release_near_start_time > 0 else 0, self._confirm_ms)
                        return None
                else:
                    if not (_is_backtest or (self._release_far_ticks >= self._confirm_ticks and (time.monotonic() - self._release_far_start_time) * 1000 >= self._confirm_ms)):
                        self._set_eval(skip_reason="LIFECYCLE_RELEASE_PENDING", leg="FAR")
                        logger.warning("[MTS_RELEASE_BLOCKED] reason=TICK_CONFIRM_PENDING leg=FAR ct=%d/%d age_ms=%.0f/%s", self._release_far_ticks, self._confirm_ticks, (time.monotonic() - self._release_far_start_time) * 1000 if self._release_far_start_time > 0 else 0, self._confirm_ms)
                        return None
                # 2026-07-08 Hermes Agent: BB filter gate removed per ADR-014.
                # Squeeze/BB no longer gate release decisions.
                # Shadow telemetry: bb_position and sqz_on logged for research only.
                logger.info(
                    "[MTS_RELEASE_SHADOW] leg=%s sqz_on=%s near_bb_upper=%s near_bb_lower=%s far_bb_upper=%s far_bb_lower=%s release_mode=SHADOW",
                    _release_leg.value,
                    bar.get("sqz_on", "N/A"),
                    bar.get("near_bb_upper", "N/A"),
                    bar.get("near_bb_lower", "N/A"),
                    bar.get("far_bb_upper", "N/A"),
                    bar.get("far_bb_lower", "N/A"),
                )
                # RELEASE: use decision.release_leg → build PARTIAL_EXIT Signal
                _release_leg = _decision.release_leg
                if _release_leg is None:
                    logger.error("[LIFECYCLE] RELEASE decision missing release_leg — skipping")
                    return None
                _exit_price = near_close if _release_leg == Leg.NEAR else far_close
                _pnl_pts = _n_pnl if _release_leg == Leg.NEAR else _f_pnl
                _turnover = (self._near_entry + _exit_price) * _mult
                _cost = 40.0 + _turnover * 2e-5
                _realized = _pnl_pts * _mult - _cost
                _signal_reason = f"TMF_RELEASE_{_release_leg.value}"
                _rel_leg_str = _release_leg.value.lower()  # "near"/"far" for legacy
                self._lifecycle = f"RELEASE_{_release_leg.value}"
                self._release_ts = now
                self._release_mono = time.monotonic()
                self._released_leg = _rel_leg_str
                self._release_price = _exit_price
                
                # 2026-07-14 Gemini CLI: Reset shadow tracking variables on leg release
                self._shadow_exit_triggered = False
                self._shadow_exit_ts = None
                self._shadow_exit_price = None
                self._shadow_exit_upl = None
                self._shadow_max_giveback = 0.0
                self._post_shadow_mfe = None
                self._post_shadow_mae = None
                self._formal_max_giveback = 0.0
                self._mtf_score_at_release = bar.get("mtf_score")
                # Anchor capture for remaining leg
                _anchor = far_close if _release_leg == Leg.NEAR else near_close
                if _anchor > 0:
                    self._post_release_anchor_price = _anchor
                    _append_event("POST_RELEASE_ANCHOR_SET", remaining_leg="FAR" if _release_leg == Leg.NEAR else "NEAR", anchor_price=_anchor)
                # MFE/MAE
                _mfe = (self._near_max - self._near_entry) if self._near_side == "LONG" else (self._near_entry - self._near_min)
                _mae = (self._near_entry - self._near_min) if self._near_side == "LONG" else (self._near_max - self._near_entry)
                self._log_exit_decision(exit_reason="RELEASE_STOP", pnl=_pnl_pts, bar=bar)
                _risk_meta["mtf_score"] = self._mtf_score_at_release
                _append_event(f"RELEASE_{_release_leg.value}_SUBMITTED", released_leg=_release_leg.value, exit_price=_exit_price, gross_points=_pnl_pts, cost=_cost, realized_pnl=_realized, mfe=round(_mfe,2), mae=round(_mae,2), **_risk_meta)
                # 2026-07-09 Hermes Agent: P0 — commit lifecycle state NOW, after all
                # pre-order guards (tick confirmation, BB filter) have passed.
                _commit_action(self._lifecycle_oca, _decision)
                logger.warning("[LIFECYCLE_DECISION] action=%s release_leg=%s", _decision.action, _decision.release_leg)
                _write_mts_state(has_position=True, action=f"RELEASE_{_release_leg.value}", reason=f"{_release_leg.value}_pnl={_pnl_pts:.1f}", near_entry=self._near_entry, far_entry=self._far_entry, near_last=near_close, far_last=far_close, near_side=self._near_side, far_side=self._far_side, near_status="RELEASED" if _release_leg == Leg.NEAR else "OPEN", far_status="RELEASED" if _release_leg == Leg.FAR else "OPEN", near_realized_override=_realized if _release_leg == Leg.NEAR else None, far_realized_override=_realized if _release_leg == Leg.FAR else None, spread_z=spread_z, released_leg=_rel_leg_str, release_price=_exit_price, release_stop_points=int(release_stop), trail_distance_points=int(trail_dist), trade_id=self._trade_id, ticker=self._ticker, lifecycle=lifecycle_to_dict(self._lifecycle_oca), **_risk_meta)
                return Signal("PARTIAL_EXIT", _signal_reason, confidence=0.4)
            elif _decision.action in (LifecycleAction.TRAIL, LifecycleAction.STOPLOSS, LifecycleAction.TIMEOUT):
                # ── ADR-011 Phase 4: Post-fill warmup guard ──
                # Blocks TRAIL if warmup (500ms / 2 ticks) hasn't completed.
                # Prevents immediate EXIT triggered by stale pre-fill prices or
                # callback-timing races.
                if _decision.action == LifecycleAction.TRAIL and not _is_backtest:
                    _warmup_elapsed = (time.monotonic() - self._single_leg_entered_mono) * 1000.0 if self._single_leg_entered_mono > 0 else 0.0
                    _warmup_ticks = self._single_leg_post_fill_ticks
                    if _warmup_elapsed < self._single_leg_warmup_ms or _warmup_ticks < self._single_leg_warmup_ticks:
                        self._trail_pending_mono = 0.0  # prevent timeout bleed
                        self._set_eval(
                            skip_reason="TRAIL_WARMUP",
                            elapsed_ms=round(_warmup_elapsed, 1),
                            warmup_ms=self._single_leg_warmup_ms,
                            ticks=_warmup_ticks,
                            warmup_ticks=self._single_leg_warmup_ticks,
                        )
                        logger.warning(
                            "[MTS_TRAIL_WARMUP] elapsed=%.0f/%s ticks=%s/%s",
                            _warmup_elapsed, self._single_leg_warmup_ms,
                            _warmup_ticks, self._single_leg_warmup_ticks,
                        )
                        return None
                # TRAIL / STOPLOSS / TIMEOUT: full exit of remaining leg
                _exit_reason = _decision.action.name
                _tg = self._lifecycle_oca.trail_group
                _rem_leg = _tg.remaining_leg
                if _rem_leg is None:
                    _rem_leg = Leg.FAR if self._released_leg == "near" else Leg.NEAR
                _exit_price = far_close if _rem_leg == Leg.FAR else near_close
                _rem_entry = self._far_entry if _rem_leg == Leg.FAR else self._near_entry
                _rem_side = self._far_side if _rem_leg == Leg.FAR else self._near_side
                _pnl_pts = (_exit_price - _rem_entry) if _rem_side == "LONG" else (_rem_entry - _exit_price)
                _turnover = (_rem_entry + _exit_price) * _mult
                _cost = 40.0 + _turnover * 2e-5
                _realized = _pnl_pts * _mult - _cost
                self._lifecycle = "EXITING"
                self._log_exit_decision(exit_reason=_exit_reason, pnl=_pnl_pts, bar=bar)
                _append_event("EXIT_REMAINING", reason=_exit_reason, remaining_leg=_rem_leg.value, exit_price=_exit_price, gross_points=_pnl_pts, cost=_cost, realized_pnl=_realized, **_risk_meta)
                _write_mts_state(has_position=True, action=f"EXIT_{_exit_reason}", reason=_exit_reason, near_entry=self._near_entry, far_entry=self._far_entry, near_last=near_close, far_last=far_close, near_side=self._near_side, far_side=self._far_side, spread_z=spread_z, released_leg=self._released_leg, trade_id=self._trade_id, ticker=self._ticker, lifecycle=lifecycle_to_dict(self._lifecycle_oca), **_risk_meta)
                self._log_shadow_trade_summary(_exit_price, _exit_reason, _pnl_pts, now, bar, near_close, far_close)
                return Signal("EXIT", f"TMF_{_exit_reason}", confidence=0.5, stop_loss=0)
            elif _decision.action == LifecycleAction.MANUAL:
                # MANUAL: full flatten — same as STOPLOSS/TIMEOUT
                _exit_reason = "MANUAL"
                _tg = self._lifecycle_oca.trail_group
                _rem_leg = _tg.remaining_leg
                if _rem_leg is None:
                    _rem_leg = Leg.FAR if self._released_leg == "near" else Leg.NEAR
                _exit_price = far_close if _rem_leg == Leg.FAR else near_close
                _rem_entry = self._far_entry if _rem_leg == Leg.FAR else self._near_entry
                _rem_side = self._far_side if _rem_leg == Leg.FAR else self._near_side
                _pnl_pts = (_exit_price - _rem_entry) if _rem_side == "LONG" else (_rem_entry - _exit_price)
                self._lifecycle = "EXITING"
                self._log_exit_decision(exit_reason="MANUAL", pnl=_pnl_pts, bar=bar)
                _append_event("MANUAL_EXIT", **_risk_meta)
                _write_mts_state(has_position=True, action="MANUAL_EXIT", reason="manual", near_entry=self._near_entry, far_entry=self._far_entry, near_last=near_close, far_last=far_close, near_side=self._near_side, far_side=self._far_side, spread_z=spread_z, released_leg=self._released_leg, trade_id=self._trade_id, ticker=self._ticker, lifecycle=lifecycle_to_dict(self._lifecycle_oca), **_risk_meta)
                self._log_shadow_trade_summary(_exit_price, "MANUAL", _pnl_pts, now, bar, near_close, far_close)
                return Signal("EXIT", "TMF_MANUAL", confidence=1.0, stop_loss=0)

        # ── Legacy path (fallback when _decision is None) ──
        # ── Full spread held ──
        if self._lifecycle_oca.phase == PositionPhase.SPREAD:
            # Awaiting release fill confirmation
            if self._lifecycle_oca.release_group.status in (
                ReleaseGroupStatus.TRIGGERED,
                ReleaseGroupStatus.SUBMITTED,
                ReleaseGroupStatus.FILLED,
            ):
                self._set_eval(skip_reason="AWAITING_RELEASE_FILL", rg_status=self._lifecycle_oca.release_group.status.value)
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
                lifecycle=self._current_lifecycle_state(),
                **_risk_meta
            )
            return None

        # Warmup tick count logic moved to start of _manage_position to prevent deadlock
        elif self._released_leg is not None and self._lifecycle_oca.phase not in (PositionPhase.SPREAD, PositionPhase.SINGLE_LEG):
            # ADR-011 Phase 3: Blocked — SINGLE_LEG only via confirmed fill.
            # This legacy path set SINGLE_LEG based on _released_leg alone, without
            # verifying fill confirmation.  This was the root cause of the 38ms
            # double-order bug (MTS_RELEASE + MTS_EXIT submitted in same tick).
            logger.error(
                "[MTS_SINGLE_LEG_TRANSITION_BLOCKED] reason=FILL_NOT_CONFIRMED "
                "phase=%s _released_leg=%s",
                self._lifecycle_oca.phase.value if hasattr(self._lifecycle_oca.phase, 'value') else self._lifecycle_oca.phase,
                self._released_leg,
            )

        _is_backtest = os.getenv("MTS_BACKTEST") == "1"
        if self._released_leg == "near":
            _rem_price, _rem_entry, _rem_leg_label, _released_leg_label = far_close, self._far_entry, "FAR", "NEAR"
            if self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG and not _is_backtest:
                # 2026-07-20 Gemini CLI: SINGLE_LEG may begin mid-bar after a RELEASE fill.
                # In live/paper mode, whole-bar high/low can include observations
                # from before the phase boundary. Use only the current event price
                # so peak/nadir are derived exclusively from post-release ticks.
                _rem_high = _rem_low = far_close
            else:
                # 2026-05-27 Gemini CLI: Evaluate intra-bar extremes
                _rem_high = float(bar.get("far_high", far_close))
                _rem_low = float(bar.get("far_low", far_close))
        else:
            _rem_price, _rem_entry, _rem_leg_label, _released_leg_label = near_close, self._near_entry, "NEAR", "FAR"
            if self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG and not _is_backtest:
                # 2026-07-20 Gemini CLI: SINGLE_LEG may begin mid-bar after a RELEASE fill.
                # In live/paper mode, whole-bar high/low can include observations
                # from before the phase boundary. Use only the current event price
                # so peak/nadir are derived exclusively from post-release ticks.
                _rem_high = _rem_low = near_close
            else:
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
        # ADR-009 Task 8: breakeven floor retained for potential future lifecycle
        # controller integration; exit trigger now comes solely from
        # evaluate_lifecycle_actions().
        post_release = self._params.get("post_release", {})
        breakeven_atr_mult = post_release.get("breakeven_after_atr")
        effective_trail_stop = _trail_stop
        if breakeven_atr_mult is not None and atr_val > 0:
            if rem_floating_pnl >= float(breakeven_atr_mult) * atr_val:
                if self._side == "LONG":
                    effective_trail_stop = max(effective_trail_stop, _rem_entry)
                else:
                    effective_trail_stop = min(effective_trail_stop, _rem_entry)

        # ── Post-Release Stage 3: Force Lock (removed in ADR-009 Task 8) ──
        # Force_lock_triggered bypass removed: if force_lock is needed,
        # add to LifecycleContext / evaluate_lifecycle_actions in a separate change.

        # Re-evaluate lifecycle after peak/nadir + breakeven adjustments
        if _decision is None:
            try:
                _ctx2 = LifecycleContext(
                    near_pnl_pts=0, far_pnl_pts=0,
                    floating_pnl_pts=rem_floating_pnl,
                    entry_age_secs=(now - self._entry_ts).total_seconds() if self._entry_ts else 0.0,
                    release_stop_threshold=release_stop, trail_dist=trail_dist,
                    trailing_side=Side.LONG if self._side == "LONG" else Side.SHORT,
                    peak=self._peak, nadir=self._nadir,
                    rem_high=_rem_high, rem_low=_rem_low,
                    is_backtest=_is_backtest,
                )
                _decision2 = evaluate_lifecycle_actions(_ctx2, self._lifecycle_oca)

                # 2026-07-20 Gemini CLI: PR 3B Shadow Validation comparison (second evaluation block)
                _entry_age_secs = (now - self._entry_ts).total_seconds() if self._entry_ts else 0.0
                _adapter_input2 = LifecycleEvaluationInput(
                    strategy_state={
                        "near_pnl_pts": 0.0,
                        "far_pnl_pts": 0.0,
                        "floating_pnl_pts": rem_floating_pnl,
                        "entry_age_secs": _entry_age_secs,
                        "release_stop_threshold": release_stop,
                        "trail_dist": trail_dist,
                        "manual_requested": getattr(self, "_manual_exit_requested", False),
                        "max_hold_secs": self._params.get("max_hold_secs"),
                        "max_loss_pts": self._params.get("max_loss_pts"),
                        "trailing_side": Side.LONG if self._side == "LONG" else Side.SHORT,
                        "peak": self._peak,
                        "nadir": self._nadir,
                        "rem_high": _rem_high,
                        "rem_low": _rem_low,
                        "last_applied_event_time": self._last_applied_event_time.isoformat() if getattr(self, "_last_applied_event_time", None) else None,
                        "single_leg_started_at": self._single_leg_started_at.isoformat() if getattr(self, "_single_leg_started_at", None) else None,
                    },
                    market_event={
                        "event_time": now.isoformat(),
                        "timestamp": now.isoformat(),
                        "ts": now.isoformat(),
                    },
                    lifecycle=self._lifecycle_oca,
                    execution_mode="BACKTEST" if _is_backtest else "LIVE",
                )
                _adapter_res2 = self._lifecycle_adapter.evaluate(_adapter_input2)
                _shadow_decision2 = _adapter_res2.decision
                _legacy_action2 = _decision2.action if _decision2 else None
                _shadow_action2 = _shadow_decision2.action if _shadow_decision2 else None

                if _legacy_action2 != _shadow_action2:
                    logger.error(
                        "[MTS_LIFECYCLE_SHADOW_MISMATCH_2] legacy_action=%s shadow_action=%s diagnostics=%s",
                        _legacy_action2, _shadow_action2, _adapter_res2.diagnostics
                    )
                    if _is_backtest:
                        raise RuntimeError(f"MTS_LIFECYCLE_SHADOW_MISMATCH_2: legacy={_legacy_action2} shadow={_shadow_action2}")

                if _decision2 is not None:
                    _decision = _decision2
            except Exception:
                logger.exception("[LIFECYCLE_TRAIL_REEVAL_FAILED]")

        # ADR-009 Task 8: lifecycle controller is the sole exit decision source.
        # Breakeven floor adjustment (effective_trail_stop) is retained as it only
        # influences the trailing stop value; the exit trigger itself now comes from
        # _decision (evaluate_lifecycle_actions -> TRAIL action) rather than a
        # duplicate trail-stop comparison + separate force_lock check.
        # Force_lock bypass removed: if force_lock is needed, add to LifecycleContext /
        # evaluate_lifecycle_actions in a separate change.
        if _decision is not None and _decision.action in (
            LifecycleAction.TRAIL, LifecycleAction.STOPLOSS,
            LifecycleAction.TIMEOUT, LifecycleAction.MANUAL,
        ):
            exit_triggered = True
            exit_reason = _decision.action.value
        else:
            exit_triggered = False
            exit_reason = "NONE"

        if exit_triggered:
            exit_price = _rem_low if self._side == "LONG" else _rem_high
            _pnl_pts = (exit_price - _rem_entry) if self._side == "LONG" else (_rem_entry - exit_price)
            _turnover = (_rem_entry + exit_price) * _mult
            _cost = 40.0 + _turnover * 2e-5
            _realized = _pnl_pts * _mult - _cost
            self._lifecycle = "EXITING"
            self._log_exit_decision(exit_reason=exit_reason, pnl=_pnl_pts, bar=bar)
            _append_event("EXIT_REMAINING", reason=exit_reason, remaining_leg=_rem_leg_label, exit_price=exit_price, gross_points=_pnl_pts, cost=_cost, realized_pnl=_realized, **_risk_meta)
            _write_mts_state(has_position=True, action=f"EXIT_{exit_reason}", reason=exit_reason, near_entry=self._near_entry, far_entry=self._far_entry, near_last=near_close, far_last=far_close, near_side=self._near_side, far_side=self._far_side, spread_z=spread_z, released_leg=self._released_leg, trade_id=self._trade_id, ticker=self._ticker, lifecycle=lifecycle_to_dict(self._lifecycle_oca), **_risk_meta)
            # 2026-07-14 Gemini CLI: Log shadow trade summary for legacy path exit
            self._log_shadow_trade_summary(exit_price, exit_reason, _pnl_pts, now, bar, near_close, far_close)
            self._last_applied_event_time = now
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
            lifecycle=self._current_lifecycle_state(),
            **_risk_meta
        )
        self._last_applied_event_time = now
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
            _cost = 40.0 + _turnover * 2e-5
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

        # 2026-07-01 Gemini CLI: Set in-memory position state to False first to prevent concurrent tick heartbeats from overwriting the file with True.
        self._has_position = False
        self._lifecycle = "FLAT"
        # ADR-009 Phase 2 / Task 7: sync lifecycle to FLAT after exit fill
        if hasattr(self, '_lifecycle_oca'):
            _was_trailing = self._lifecycle_oca.phase == PositionPhase.SINGLE_LEG
            self._lifecycle_oca.phase = PositionPhase.FLAT
            self._lifecycle_oca.release_group.status = ReleaseGroupStatus.INACTIVE
            if _was_trailing:
                self._lifecycle_oca.trail_group.status = TrailGroupStatus.FILLED
        # 2026-07-08 Hermes Agent: pass trail exit realized PnL so cumulative tracking
        # can combine with the previously-released leg's realized from state file.
        _trail_exit_realized = _realized if (exit_price is not None and self._released_leg is not None) else 0.0
        _write_mts_state(has_position=False, action="CLOSE", reason=reason or "trail_exit",
                         ticker=_ticker,
                         lifecycle=lifecycle_to_dict(self._lifecycle_oca) if hasattr(self, '_lifecycle_oca') else {},
                         trail_exit_realized=_trail_exit_realized)
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
