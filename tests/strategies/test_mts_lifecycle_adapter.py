# 2026-07-20 Gemini CLI: unit tests for MtsLifecycleAdapter
import pytest
from datetime import datetime, timedelta

from strategies.plugins.futures.active.tmf_spread import (
    Leg,
    Side,
    PositionPhase,
    ReleaseGroupStatus,
    TrailGroupStatus,
    LifecycleAction,
    PositionLifecycle,
    LifecycleDecision,
)

from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    calculate_peak_giveback_ratio,
    ContextBuildStatus,
    RecoveryStatus,
    LifecycleEvaluationInput,
    LifecycleRecoveryFacts,
    MtsLifecycleAdapter,
)

def test_calculate_peak_giveback_ratio_invariants():
    """Verify PeakGivebackRatio edge cases and mathematical guard constraints."""
    # 1. Normal positive peaks
    assert calculate_peak_giveback_ratio(100.0, 40.0) == 0.6
    assert calculate_peak_giveback_ratio(10.0, 10.0) == 0.0
    
    # 2. Peak is zero (returns None/NA)
    assert calculate_peak_giveback_ratio(0.0, -10.0) is None
    assert calculate_peak_giveback_ratio(0.0, 0.0) is None
    
    # 3. Peak is negative (returns None/NA)
    assert calculate_peak_giveback_ratio(-1.0, -2.0) is None
    assert calculate_peak_giveback_ratio(-50.0, -10.0) is None


def test_build_context_missing_event_time():
    """Verify built context handles missing event times correctly."""
    adapter = MtsLifecycleAdapter()
    lc = PositionLifecycle(phase=PositionPhase.SPREAD)
    evaluation_input = LifecycleEvaluationInput(
        strategy_state={},
        market_event={},  # missing ts/timestamp/event_time
        lifecycle=lc,
        execution_mode="LIVE"
    )
    ctx, diag = adapter.build_context(evaluation_input)
    assert ctx is None
    assert diag.build_status == ContextBuildStatus.MISSING_EVENT_TIME


def test_build_context_temporal_guards():
    """Verify clock regression and duplicate tick handling in live/paper mode."""
    adapter = MtsLifecycleAdapter()
    lc = PositionLifecycle(phase=PositionPhase.SPREAD)
    
    base_time = datetime(2026, 7, 20, 10, 0, 0)
    
    # Setup state with last_applied_event_time
    state = {"last_applied_event_time": base_time.isoformat()}
    
    # 1. Clock regression (event_time < last_applied_event_time)
    eval_regression = LifecycleEvaluationInput(
        strategy_state=state,
        market_event={"event_time": (base_time - timedelta(seconds=1)).isoformat()},
        lifecycle=lc,
        execution_mode="LIVE"
    )
    ctx, diag = adapter.build_context(eval_regression)
    assert ctx is None
    assert diag.build_status == ContextBuildStatus.CLOCK_REGRESSION
    
    # 2. Duplicate event (event_time == last_applied_event_time)
    eval_duplicate = LifecycleEvaluationInput(
        strategy_state=state,
        market_event={"event_time": base_time.isoformat()},
        lifecycle=lc,
        execution_mode="LIVE"
    )
    ctx, diag = adapter.build_context(eval_duplicate)
    assert ctx is None
    assert diag.build_status == ContextBuildStatus.DUPLICATE_EVENT
    
    # 3. Permissive duplicate check in BACKTEST mode
    eval_backtest = LifecycleEvaluationInput(
        strategy_state=state,
        market_event={"event_time": base_time.isoformat()},
        lifecycle=lc,
        execution_mode="BACKTEST"
    )
    ctx, diag = adapter.build_context(eval_backtest)
    assert diag.build_status == ContextBuildStatus.VALID
    assert ctx is not None


def test_build_context_single_leg_anchors():
    """Verify single-leg anchor validation rules."""
    adapter = MtsLifecycleAdapter()
    lc = PositionLifecycle(phase=PositionPhase.SINGLE_LEG)
    
    base_time = datetime(2026, 7, 20, 10, 0, 0)
    
    # 1. Missing anchor
    eval_missing_anchor = LifecycleEvaluationInput(
        strategy_state={},
        market_event={"event_time": base_time.isoformat()},
        lifecycle=lc,
        execution_mode="LIVE"
    )
    ctx, diag = adapter.build_context(eval_missing_anchor)
    assert ctx is None
    assert diag.build_status == ContextBuildStatus.MISSING_ANCHOR
    
    # 2. Pre-single-leg event time (event_time < single_leg_started_at)
    started_at = base_time + timedelta(seconds=10)
    eval_pre_single_leg = LifecycleEvaluationInput(
        strategy_state={"single_leg_started_at": started_at.isoformat()},
        market_event={"event_time": base_time.isoformat()},
        lifecycle=lc,
        execution_mode="LIVE"
    )
    ctx, diag = adapter.build_context(eval_pre_single_leg)
    assert ctx is None
    assert diag.build_status == ContextBuildStatus.PRE_SINGLE_LEG_EVENT
    
    # 3. Valid single-leg event
    eval_valid = LifecycleEvaluationInput(
        strategy_state={"single_leg_started_at": base_time.isoformat()},
        market_event={"event_time": (base_time + timedelta(seconds=5)).isoformat()},
        lifecycle=lc,
        execution_mode="LIVE"
    )
    ctx, diag = adapter.build_context(eval_valid)
    assert diag.build_status == ContextBuildStatus.VALID
    assert ctx is not None


def test_adapter_evaluation():
    """Verify evaluate method handles result formatting correctly."""
    adapter = MtsLifecycleAdapter()
    lc = PositionLifecycle(phase=PositionPhase.SPREAD)
    lc.release_group.status = ReleaseGroupStatus.ARMED
    
    base_time = datetime(2026, 7, 20, 10, 0, 0)
    eval_input = LifecycleEvaluationInput(
        strategy_state={
            "near_pnl_pts": -25.0,  # hit stop threshold
            "far_pnl_pts": 0.0,
            "release_stop_threshold": 20.0,
        },
        market_event={"event_time": base_time.isoformat()},
        lifecycle=lc,
        execution_mode="LIVE"
    )
    result = adapter.evaluate(eval_input)
    assert result.context is not None
    assert result.decision is not None
    assert result.decision.action == LifecycleAction.RELEASE
    assert result.decision.release_leg == Leg.NEAR
    assert result.diagnostics.build_status == ContextBuildStatus.VALID


def test_adapter_recovery_facts():
    """Verify precedence rules in adapter recover projection."""
    adapter = MtsLifecycleAdapter()
    lc = PositionLifecycle(phase=PositionPhase.SINGLE_LEG)
    
    # 1. Exact recovery consensus
    facts_exact = LifecycleRecoveryFacts(
        persisted_lifecycle=lc,
        release_fill={"leg": "FAR"},
        remaining_position={"qty": 1},
        persisted_extrema={"peak": 100.0, "nadir": 50.0}
    )
    proj = adapter.recover(facts_exact)
    assert proj.recovery_status == RecoveryStatus.EXACT
    assert proj.peak == 100.0
    assert proj.nadir == 50.0
    
    # 2. Persisted extrema only
    facts_persisted = LifecycleRecoveryFacts(
        persisted_lifecycle=lc,
        remaining_position={"qty": 1},
        persisted_extrema={"peak": 105.0}
    )
    proj = adapter.recover(facts_persisted)
    assert proj.recovery_status == RecoveryStatus.PERSISTED
    assert proj.peak == 105.0
    
    # 3. Replay reconstruction from fills
    facts_replay = LifecycleRecoveryFacts(
        persisted_lifecycle=lc,
        release_fill={"leg": "FAR"},
        remaining_position={"qty": 1}
    )
    proj = adapter.recover(facts_replay)
    assert proj.recovery_status == RecoveryStatus.REPLAYED
    
    # 4. Degraded broker positions only
    facts_degraded = LifecycleRecoveryFacts(
        persisted_lifecycle=lc,
        remaining_position={"qty": 1}
    )
    proj = adapter.recover(facts_degraded)
    assert proj.recovery_status == RecoveryStatus.DEGRADED
    
    # 5. Unrecoverable (missing position in single leg)
    facts_unrecoverable = LifecycleRecoveryFacts(
        persisted_lifecycle=lc
    )
    proj = adapter.recover(facts_unrecoverable)
    assert proj.recovery_status == RecoveryStatus.UNRECOVERABLE
