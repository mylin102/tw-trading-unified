# 2026-07-20 Gemini CLI: pure adapter Anti-Corruption Layer for MTS lifecycle
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any
import pandas as pd

logger = logging.getLogger(__name__)

class Leg(Enum):
    """Spread leg identifier."""
    NEAR = "NEAR"
    FAR = "FAR"

class Side(Enum):
    """Position side (direction)."""
    LONG = "LONG"
    SHORT = "SHORT"

class PositionPhase(Enum):
    """Shape of the spread position (not order progress)."""
    FLAT = "FLAT"           # no position
    SPREAD = "SPREAD"       # both legs held
    SINGLE_LEG = "SINGLE_LEG"  # one leg released, one remaining

class ReleaseGroupStatus(Enum):
    """Lifecycle of the release OCO pair.

    ADR-010: expanded with SUBMITTING, PARTIALLY_FILLED, CANCELING_SIBLING,
    SIBLING_CANCELED for broker-level OCO bracket support.
    """
    INACTIVE = "INACTIVE"               # not in spread phase
    ARMED = "ARMED"                     # spread held, monitoring release_stop
    TRIGGERED = "TRIGGERED"             # release_stop hit, about to submit
    SUBMITTING = "SUBMITTING"           # one order submitted, second in flight (restartable)
    SUBMITTED = "SUBMITTED"             # both release orders submitted
    FILLED = "FILLED"                   # one leg filled, sibling cancel pending
    PARTIALLY_FILLED = "PARTIALLY_FILLED"  # one leg filled, sibling cancel in flight
    CANCELING_SIBLING = "CANCELING_SIBLING"  # cancel submitted, awaiting confirmation
    SIBLING_CANCELED = "SIBLING_CANCELED"    # sibling cancel confirmed, safe to trail
    COMPLETED = "COMPLETED"             # filled + sibling cancel confirmed
    FAILED = "FAILED"                   # terminal failure

class TrailGroupStatus(Enum):
    """Lifecycle of the post-release single-leg trailing exit."""
    INACTIVE = "INACTIVE"   # not in single-leg phase
    ARMED = "ARMED"         # release confirmed, trail not yet active
    ACTIVE = "ACTIVE"       # trail stop calculated and monitored
    SUBMITTED = "SUBMITTED" # exit order submitted
    FILLED = "FILLED"       # exit fill confirmed
    FAILED = "FAILED"       # terminal failure

class LifecycleAction(Enum):
    """Action selected by the lifecycle controller."""
    MANUAL = "MANUAL"
    STOPLOSS = "STOPLOSS"
    TIMEOUT = "TIMEOUT"
    RELEASE = "RELEASE"
    TRAIL = "TRAIL"

class CancelStatus(Enum):
    """Status of a sibling cancel order (ADR-010)."""
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    REJECTED = "REJECTED"

@dataclass
class EntryRiskSnapshot:
    """Snapshot of risk parameters at entry time for OCO bracket (ADR-010).

    Immutable after creation. Prevents ATR drift during bracket lifetime.
    """
    atr: float = 0.0
    release_stop: float = 0.0
    trail_stop: float = 0.0
    entry_z: float = 0.0
    spread: float = 0.0
    timestamp: str = ""  # ISO datetime

@dataclass
class ReleaseGroup:
    """State holder for the release OCO pair (ADR-009 v1.1, expanded ADR-010)."""
    status: ReleaseGroupStatus = ReleaseGroupStatus.INACTIVE
    near_order_id: str | None = None
    far_order_id: str | None = None
    filled_leg: Leg | None = None       # IS the winner_leg (ADR-010)
    filled_order_id: str | None = None
    canceled_leg: Leg | None = None     # IS the loser leg (ADR-010)
    trigger_ts: str | None = None

    # ADR-010: sibling cancel tracking
    sibling_cancel_order_id: str | None = None
    sibling_cancel_status: CancelStatus | None = None

    # ADR-010: entry risk snapshot
    entry_risk: EntryRiskSnapshot | None = None

    # ADR-010: release order metadata (source of truth for orders export)
    near_price: float = 0.0
    far_price: float = 0.0
    near_side: str | None = None
    far_side: str | None = None
    order_type: str = "MKP"

@dataclass
class TrailGroup:
    """State holder for the post-release trailing exit (ADR-009 v1.1)."""
    status: TrailGroupStatus = TrailGroupStatus.INACTIVE
    remaining_leg: Leg | None = None
    exit_order_id: str | None = None
    peak_pnl: float | None = None
    nadir_pnl: float | None = None
    trail_stop: float | None = None
    trigger_ts: str | None = None

@dataclass
class PositionLifecycle:
    """Aggregate state for the MTS spread position lifecycle (ADR-009 v1.1)."""
    phase: PositionPhase = PositionPhase.FLAT
    release_group: ReleaseGroup = field(default_factory=ReleaseGroup)
    trail_group: TrailGroup = field(default_factory=TrailGroup)

@dataclass
class LifecycleDecision:
    """Result of evaluate_lifecycle_actions()."""
    action: LifecycleAction
    release_leg: Leg | None = None  # which leg to release (only for RELEASE)

logger = logging.getLogger(__name__)

class ContextBuildStatus(Enum):
    VALID = "VALID"
    DUPLICATE_EVENT = "DUPLICATE_EVENT"
    PRE_SINGLE_LEG_EVENT = "PRE_SINGLE_LEG_EVENT"
    CLOCK_REGRESSION = "CLOCK_REGRESSION"
    MISSING_EVENT_TIME = "MISSING_EVENT_TIME"
    MISSING_ANCHOR = "MISSING_ANCHOR"
    INCONSISTENT_STATE = "INCONSISTENT_STATE"

class RecoveryStatus(Enum):
    EXACT = "EXACT"
    PERSISTED = "PERSISTED"
    REPLAYED = "REPLAYED"
    DEGRADED = "DEGRADED"
    UNRECOVERABLE = "UNRECOVERABLE"

@dataclass
class LifecycleContext:
    """Input data for evaluate_lifecycle_actions(). Pure data — no filesystem, no Shioaji."""
    near_pnl_pts: float
    far_pnl_pts: float
    floating_pnl_pts: float
    entry_age_secs: float
    release_stop_threshold: float
    trail_dist: float
    manual_requested: bool = False
    max_hold_secs: float | None = None        # None = no timeout
    max_loss_pts: float | None = None          # None = no stoploss
    trailing_side: Side | None = None          # LONG/SHORT for remaining leg
    peak: float = 0.0
    nadir: float = 0.0
    rem_high: float = 0.0
    rem_low: float = 0.0
    is_backtest: bool = False

@dataclass(frozen=True)
class LifecycleEvaluationInput:
    strategy_state: dict[str, Any]
    market_event: dict[str, Any]
    lifecycle: PositionLifecycle
    execution_mode: str  # "LIVE", "PAPER", "BACKTEST"

@dataclass(frozen=True)
class LifecycleDiagnostics:
    build_status: ContextBuildStatus
    rejection_reason: str | None = None
    event_time: datetime | None = None
    received_at: datetime | None = None
    processed_at: datetime = field(default_factory=datetime.now)
    lifecycle_phase: str = "FLAT"
    event_sequence: int | None = None
    provenance_source: str | None = None
    time_above_be_seconds: float = 0.0

@dataclass(frozen=True)
class LifecycleEvaluationResult:
    context: LifecycleContext | None = None  # None if context build is rejected
    decision: LifecycleDecision | None = None
    diagnostics: LifecycleDiagnostics | None = None

@dataclass(frozen=True)
class LifecycleRecoveryFacts:
    persisted_lifecycle: PositionLifecycle
    release_fill: dict[str, Any] | None = None
    remaining_position: dict[str, Any] | None = None
    persisted_extrema: dict[str, Any] | None = None
    latest_valid_market_event: dict[str, Any] | None = None

@dataclass(frozen=True)
class RecoveredLifecycleProjection:
    lifecycle: PositionLifecycle
    peak: float | None = None
    nadir: float | None = None
    recovery_status: RecoveryStatus = RecoveryStatus.UNRECOVERABLE
    evidence: list[str] = field(default_factory=list)

def enum_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if hasattr(value, "value"):
        return str(value.value)
    return str(value)

def calculate_peak_giveback_ratio(peak_rem_net_pnl: float, exit_rem_net_pnl: float) -> float | None:
    # 2026-07-20 Gemini CLI: PeakGivebackRatio invariant gate
    if peak_rem_net_pnl <= 0:
        return None
    return (peak_rem_net_pnl - exit_rem_net_pnl) / peak_rem_net_pnl

def _check_manual_candidate(ctx: LifecycleContext) -> list[LifecycleDecision]:
    if ctx.manual_requested:
        return [LifecycleDecision(action=LifecycleAction.MANUAL)]
    return []

def _check_stoploss_candidate(ctx: LifecycleContext) -> list[LifecycleDecision]:
    if ctx.max_loss_pts is not None and ctx.floating_pnl_pts <= -ctx.max_loss_pts:
        return [LifecycleDecision(action=LifecycleAction.STOPLOSS)]
    return []

def _check_timeout_candidate(ctx: LifecycleContext) -> list[LifecycleDecision]:
    if ctx.max_hold_secs is not None and ctx.entry_age_secs > ctx.max_hold_secs:
        return [LifecycleDecision(action=LifecycleAction.TIMEOUT)]
    return []

def _check_release_candidates(
    ctx: LifecycleContext, lifecycle: PositionLifecycle,
) -> list[LifecycleDecision]:
    _phase_val = enum_value(lifecycle.phase)
    if _phase_val != "SPREAD":
        logger.info(
            "[CHECK_RELEASE_SKIP] phase=%s expected=SPREAD rg_status=%s near_pnl=%.1f far_pnl=%.1f",
            _phase_val,
            enum_value(lifecycle.release_group.status),
            ctx.near_pnl_pts, ctx.far_pnl_pts,
        )
        return []
    _rg_status = enum_value(lifecycle.release_group.status)
    if _rg_status not in ("ARMED", "TRIGGERED"):
        logger.info(
            "[CHECK_RELEASE_SKIP] phase=%s rg_status=%s expected in (ARMED,TRIGGERED) near_pnl=%.1f far_pnl=%.1f",
            _phase_val,
            _rg_status,
            ctx.near_pnl_pts, ctx.far_pnl_pts,
        )
        return []
    near_hit = ctx.near_pnl_pts <= -ctx.release_stop_threshold
    far_hit = ctx.far_pnl_pts <= -ctx.release_stop_threshold
    if near_hit or far_hit:
        # If both hit, release the more negative leg
        if near_hit and far_hit:
            leg = Leg.NEAR if ctx.near_pnl_pts <= ctx.far_pnl_pts else Leg.FAR
        elif near_hit:
            leg = Leg.NEAR
        else:
            leg = Leg.FAR
        logger.info(
            "[CHECK_RELEASE_DECISION] near_hit=%s far_hit=%s action=RELEASE leg=%s "
            "near_pnl=%.1f far_pnl=%.1f threshold=%.1f",
            near_hit, far_hit, leg.value,
            ctx.near_pnl_pts, ctx.far_pnl_pts, ctx.release_stop_threshold,
        )
        return [LifecycleDecision(action=LifecycleAction.RELEASE, release_leg=leg)]
    logger.info(
        "[CHECK_RELEASE_SKIP] near_hit=%s far_hit=%s "
        "near_pnl=%.1f far_pnl=%.1f threshold=%.1f",
        near_hit, far_hit,
        ctx.near_pnl_pts, ctx.far_pnl_pts, ctx.release_stop_threshold,
    )
    return []

def _check_trail_candidate(
    ctx: LifecycleContext, lifecycle: PositionLifecycle,
) -> list[LifecycleDecision]:
    _phase_val = enum_value(lifecycle.phase)
    if _phase_val != "SINGLE_LEG":
        return []
    _tg_status = enum_value(lifecycle.trail_group.status)
    if _tg_status not in ("ARMED", "ACTIVE"):
        return []
    if ctx.trailing_side is None or ctx.rem_high <= 0 or ctx.rem_low <= 0:
        return []
    if ctx.trailing_side == Side.LONG:
        if ctx.rem_low <= ctx.peak - ctx.trail_dist:
            return [LifecycleDecision(action=LifecycleAction.TRAIL)]
    else: # SHORT
        if ctx.rem_high >= ctx.nadir + ctx.trail_dist:
            return [LifecycleDecision(action=LifecycleAction.TRAIL)]
    return []

_LIFECYCLE_ACTION_PRIORITY = [
    LifecycleAction.MANUAL,
    LifecycleAction.STOPLOSS,
    LifecycleAction.TIMEOUT,
    LifecycleAction.RELEASE,
    LifecycleAction.TRAIL,
]

def evaluate_lifecycle_actions(
    ctx: LifecycleContext,
    lifecycle: PositionLifecycle,
) -> LifecycleDecision | None:
    """Pure decision engine: collect candidates → select by priority → commit."""
    _rg_status = enum_value(lifecycle.release_group.status)
    if _rg_status in ("SUBMITTED", "FILLED"):
        return None
    _tg_status = enum_value(lifecycle.trail_group.status)
    if _tg_status in ("SUBMITTED", "FILLED"):
        return None

    candidates: list[LifecycleDecision] = []
    candidates.extend(_check_manual_candidate(ctx))
    candidates.extend(_check_stoploss_candidate(ctx))
    candidates.extend(_check_timeout_candidate(ctx))
    candidates.extend(_check_release_candidates(ctx, lifecycle))
    candidates.extend(_check_trail_candidate(ctx, lifecycle))

    if not candidates:
        return None

    for priority_action in _LIFECYCLE_ACTION_PRIORITY:
        for decision in candidates:
            if decision.action == priority_action:
                return decision
    return None

class MtsLifecycleAdapter:
    def build_context(self, evaluation_input: LifecycleEvaluationInput) -> tuple[LifecycleContext | None, LifecycleDiagnostics]:
        state = evaluation_input.strategy_state
        bar = evaluation_input.market_event
        lc = evaluation_input.lifecycle
        mode = evaluation_input.execution_mode
        
        # 1. Parse event time from market event
        event_time_raw = bar.get("event_time") or bar.get("timestamp") or bar.get("ts")
        if event_time_raw is None:
            diag = LifecycleDiagnostics(
                build_status=ContextBuildStatus.MISSING_EVENT_TIME,
                rejection_reason="No timestamp or event_time in market_event",
                lifecycle_phase=enum_value(lc.phase) or "FLAT",
            )
            return None, diag
            
        event_time = None
        if isinstance(event_time_raw, datetime):
            event_time = event_time_raw
        elif isinstance(event_time_raw, str):
            try:
                event_time = datetime.fromisoformat(event_time_raw)
            except ValueError:
                try:
                    event_time = pd.Timestamp(event_time_raw).to_pydatetime()
                except Exception:
                    pass
        elif hasattr(event_time_raw, "to_pydatetime"):
            event_time = event_time_raw.to_pydatetime()
        else:
            try:
                event_time = pd.Timestamp(event_time_raw).to_pydatetime()
            except Exception:
                pass
                
        if event_time is None:
            diag = LifecycleDiagnostics(
                build_status=ContextBuildStatus.MISSING_EVENT_TIME,
                rejection_reason="Unparseable timestamp from event_time_raw",
                lifecycle_phase=enum_value(lc.phase) or "FLAT",
            )
            return None, diag
            
        # 2. Check temporal invariants (received/processed times are for diagnostics only)
        last_applied_raw = state.get("last_applied_event_time")
        last_applied = None
        if last_applied_raw:
            if isinstance(last_applied_raw, datetime):
                last_applied = last_applied_raw
            else:
                try:
                    last_applied = datetime.fromisoformat(last_applied_raw)
                except ValueError:
                    try:
                        last_applied = pd.Timestamp(last_applied_raw).to_pydatetime()
                    except Exception:
                        pass
                        
        is_backtest = (mode == "BACKTEST")
        if last_applied and not is_backtest:
            if event_time < last_applied:
                diag = LifecycleDiagnostics(
                    build_status=ContextBuildStatus.CLOCK_REGRESSION,
                    rejection_reason=f"Event time {event_time} regressed behind last applied {last_applied}",
                    event_time=event_time,
                    lifecycle_phase=enum_value(lc.phase) or "FLAT",
                )
                return None, diag
            elif event_time == last_applied:
                diag = LifecycleDiagnostics(
                    build_status=ContextBuildStatus.DUPLICATE_EVENT,
                    rejection_reason=f"Duplicate event_time {event_time}",
                    event_time=event_time,
                    lifecycle_phase=enum_value(lc.phase) or "FLAT",
                )
                return None, diag

        phase_str = enum_value(lc.phase)
        if phase_str == "SINGLE_LEG":
            started_at_raw = state.get("single_leg_started_at") or getattr(lc, "single_leg_started_at", None)
            started_at = None
            if started_at_raw:
                if isinstance(started_at_raw, datetime):
                    started_at = started_at_raw
                else:
                    try:
                        started_at = datetime.fromisoformat(started_at_raw)
                    except ValueError:
                        try:
                            started_at = pd.Timestamp(started_at_raw).to_pydatetime()
                        except Exception:
                            pass
                            
            if started_at is None:
                diag = LifecycleDiagnostics(
                    build_status=ContextBuildStatus.MISSING_ANCHOR,
                    rejection_reason="No single_leg_started_at anchor found in SINGLE_LEG phase",
                    event_time=event_time,
                    lifecycle_phase=phase_str,
                )
                return None, diag
                
            if not is_backtest and event_time < started_at:
                diag = LifecycleDiagnostics(
                    build_status=ContextBuildStatus.PRE_SINGLE_LEG_EVENT,
                    rejection_reason=f"Event time {event_time} is earlier than single-leg anchor {started_at}",
                    event_time=event_time,
                    lifecycle_phase=phase_str,
                )
                return None, diag

        # 3. Pull required parameters to construct LifecycleContext
        near_pnl = float(state.get("near_pnl_pts", 0.0))
        far_pnl = float(state.get("far_pnl_pts", 0.0))
        floating_pnl = float(state.get("floating_pnl_pts", 0.0))
        entry_age = float(state.get("entry_age_secs", 0.0))
        release_stop = float(state.get("release_stop_threshold", 20.0))
        trail_dist = float(state.get("trail_dist", 20.0))
        manual_requested = bool(state.get("manual_requested", False))
        max_hold_secs = state.get("max_hold_secs")
        max_loss_pts = state.get("max_loss_pts")
        
        trailing_side_raw = state.get("trailing_side")
        trailing_side = None
        if trailing_side_raw:
            if isinstance(trailing_side_raw, Side):
                trailing_side = trailing_side_raw
            else:
                try:
                    trailing_side = Side(trailing_side_raw)
                except ValueError:
                    pass
                    
        peak = float(state.get("peak", 0.0))
        nadir = float(state.get("nadir", 0.0))
        rem_high = float(state.get("rem_high", 0.0))
        rem_low = float(state.get("rem_low", 0.0))
        
        ctx = LifecycleContext(
            near_pnl_pts=near_pnl,
            far_pnl_pts=far_pnl,
            floating_pnl_pts=floating_pnl,
            entry_age_secs=entry_age,
            release_stop_threshold=release_stop,
            trail_dist=trail_dist,
            manual_requested=manual_requested,
            max_hold_secs=max_hold_secs,
            max_loss_pts=max_loss_pts,
            trailing_side=trailing_side,
            peak=peak,
            nadir=nadir,
            rem_high=rem_high,
            rem_low=rem_low,
            is_backtest=is_backtest,
        )
        
        # Calculate time above breakeven
        time_above_be = float(state.get("time_above_be_seconds", 0.0))
        diag = LifecycleDiagnostics(
            build_status=ContextBuildStatus.VALID,
            event_time=event_time,
            received_at=datetime.fromtimestamp(bar.get("received_at_ts")) if bar.get("received_at_ts") else None,
            processed_at=datetime.now(),
            lifecycle_phase=phase_str or "FLAT",
            event_sequence=bar.get("sequence"),
            time_above_be_seconds=time_above_be,
        )
        
        return ctx, diag

    def evaluate(self, evaluation_input: LifecycleEvaluationInput) -> LifecycleEvaluationResult:
        ctx, diag = self.build_context(evaluation_input)
        if diag.build_status != ContextBuildStatus.VALID or ctx is None:
            return LifecycleEvaluationResult(
                context=None,
                decision=None,
                diagnostics=diag
            )
            
        decision = evaluate_lifecycle_actions(ctx, evaluation_input.lifecycle)
        return LifecycleEvaluationResult(
            context=ctx,
            decision=decision,
            diagnostics=diag
        )

    def recover(self, facts: LifecycleRecoveryFacts) -> RecoveredLifecycleProjection:
        lc = facts.persisted_lifecycle
        peak = None
        nadir = None
        status = RecoveryStatus.UNRECOVERABLE
        evidence = []
        
        # Precedence 1: Committed persisted extrema
        extrema = facts.persisted_extrema
        if extrema:
            peak = extrema.get("peak")
            nadir = extrema.get("nadir")
            status = RecoveryStatus.PERSISTED
            evidence.append("persisted_extrema")
            
        # Precedence 2: Confirmed release fill + active position records
        fill = facts.release_fill
        pos = facts.remaining_position
        
        if fill and pos and status == RecoveryStatus.UNRECOVERABLE:
            status = RecoveryStatus.REPLAYED
            evidence.append("release_fill_reconstruction")
            
        # Precedence 3: Active position verified, but extrema/anchor missing (Degraded)
        if pos and not extrema and not fill:
            status = RecoveryStatus.DEGRADED
            evidence.append("degraded_broker_position")
            
        # Consensus check
        if extrema and fill and pos:
            status = RecoveryStatus.EXACT
            evidence.append("full_consensus")
            
        # Inconsistent checks
        if not pos and enum_value(lc.phase) == "SINGLE_LEG":
            status = RecoveryStatus.UNRECOVERABLE
            evidence.append("missing_position_in_single_leg")
            
        return RecoveredLifecycleProjection(
            lifecycle=lc,
            peak=peak,
            nadir=nadir,
            recovery_status=status,
            evidence=evidence
        )
