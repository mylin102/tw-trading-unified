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


def test_policy_j_primary_exit_and_fallback():
    """Verify Policy J COMBINED_EXIT triggers first on total UPL peak giveback, and falls back to RELEASE if UPL target not reached."""
    from strategies.plugins.futures.active.mts_lifecycle_adapter import (
        LifecycleContext,
        evaluate_lifecycle_actions,
        ReleaseGroup,
    )

    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED)
    )

    # 1. Policy J Primary Trigger: Target peak 400.0 TWD reached, current net exit UPL drops to 108.0 TWD (giveback 292.0 >= 100.0)
    ctx_policy_j = LifecycleContext(
        near_pnl_pts=10.0,
        far_pnl_pts=10.0,
        floating_pnl_pts=20.0,
        entry_age_secs=100.0,
        release_stop_threshold=80.0,
        trail_dist=48.9,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=300.0,
        combined_upl_giveback_twd=100.0,
        peak_net_exit_pnl_twd=400.0,
    )
    decision = evaluate_lifecycle_actions(ctx_policy_j, lc)
    assert decision is not None
    assert decision.action == LifecycleAction.COMBINED_EXIT

    # 2. Secondary Fallback: Target peak not reached (peak = 150.0 < 300.0), but single leg hits release threshold (-80.0)
    ctx_fallback = LifecycleContext(
        near_pnl_pts=-85.0,
        far_pnl_pts=10.0,
        floating_pnl_pts=-75.0,
        entry_age_secs=100.0,
        release_stop_threshold=80.0,
        trail_dist=48.9,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=300.0,
        combined_upl_giveback_twd=100.0,
        peak_net_exit_pnl_twd=150.0,
    )
    decision_fallback = evaluate_lifecycle_actions(ctx_fallback, lc)
    assert decision_fallback is not None
    assert decision_fallback.action == LifecycleAction.RELEASE
    assert decision_fallback.release_leg == Leg.NEAR



def test_policy_j_single_leg_combined_pnl_exit():
    """2026-08-03 Gemini CLI: Verify Policy J evaluation on combined total PnL during SINGLE_LEG phase."""
    from strategies.plugins.futures.active.tmf_spread import LifecycleContext
    from strategies.plugins.futures.active.mts_lifecycle_adapter import evaluate_lifecycle_actions
    lc = PositionLifecycle(phase=PositionPhase.SINGLE_LEG)
    lc.release_group.status = ReleaseGroupStatus.COMPLETED
    lc.trail_group.status = TrailGroupStatus.ARMED
    ctx_single_leg_pj = LifecycleContext(
        near_pnl_pts=-100.0,
        far_pnl_pts=124.0,
        floating_pnl_pts=124.0,
        entry_age_secs=200.0,
        release_stop_threshold=80.0,
        trail_dist=50.0,
        trailing_side=Side.LONG,
        peak=43500.0,
        nadir=43000.0,
        rem_high=43500.0,
        rem_low=43480.0,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=200.0,
        combined_upl_giveback_twd=50.0,
        peak_net_exit_pnl_twd=208.0,
    )
    decision = evaluate_lifecycle_actions(ctx_single_leg_pj, lc)
    assert decision is not None
    assert decision.action == LifecycleAction.TRAIL


def test_policy_j_fixed_target_exits_immediately_without_giveback_wait():
    """2026-10-29 Hermes Agent: fixed_target mode must exit the instant the
    activation target is first reached, with no retrace/giveback wait, and
    must label the decision POLICY_J_FIXED_TARGET (not SPREAD_GIVEBACK)."""
    from strategies.plugins.futures.active.mts_lifecycle_adapter import (
        LifecycleContext,
        evaluate_lifecycle_actions,
        ReleaseGroup,
        PolicyJTriggerPath,
    )

    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    # total_net_pnl_twd = (near+far)*10 - 92 = (15+15)*10 - 92 = 208.0 >= 200.0
    # peak is deliberately left at 0.0 (never updated) -- fixed_target must
    # not depend on peak tracking at all.
    ctx = LifecycleContext(
        near_pnl_pts=15.0,
        far_pnl_pts=15.0,
        floating_pnl_pts=30.0,
        entry_age_secs=5.0,
        release_stop_threshold=80.0,
        trail_dist=48.9,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=200.0,
        combined_upl_giveback_twd=50.0,
        combined_upl_exit_mode="fixed_target",
        peak_net_exit_pnl_twd=0.0,
    )
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is not None
    assert decision.action == LifecycleAction.COMBINED_EXIT
    assert decision.winner == PolicyJTriggerPath.FIXED_TARGET.value


def test_policy_j_fixed_target_does_not_fire_below_activation():
    """fixed_target must stay silent (fall through to other candidates)
    while total_net_pnl_twd is below the activation threshold."""
    from strategies.plugins.futures.active.mts_lifecycle_adapter import (
        LifecycleContext,
        evaluate_lifecycle_actions,
        ReleaseGroup,
    )

    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    # total_net_pnl_twd = (5+5)*10 - 92 = -92.0 (well below 200.0 activation)
    ctx = LifecycleContext(
        near_pnl_pts=5.0,
        far_pnl_pts=5.0,
        floating_pnl_pts=10.0,
        entry_age_secs=5.0,
        release_stop_threshold=80.0,
        trail_dist=48.9,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=200.0,
        combined_upl_giveback_twd=50.0,
        combined_upl_exit_mode="fixed_target",
        peak_net_exit_pnl_twd=0.0,
    )
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is None or decision.action != LifecycleAction.COMBINED_EXIT


def test_policy_j_fixed_target_single_leg_phase_returns_trail():
    """fixed_target in SINGLE_LEG phase must use TRAIL (same action-type
    convention as the existing giveback SINGLE_LEG path), not COMBINED_EXIT."""
    from strategies.plugins.futures.active.mts_lifecycle_adapter import (
        LifecycleContext,
        evaluate_lifecycle_actions,
        PolicyJTriggerPath,
    )

    lc = PositionLifecycle(phase=PositionPhase.SINGLE_LEG)
    lc.release_group.status = ReleaseGroupStatus.COMPLETED
    lc.trail_group.status = TrailGroupStatus.ARMED
    # total_net_pnl_twd = (-100+124)*10 - 92 = 148.0 ... need >=200 to fire;
    # use far_pnl_pts=130 instead: (-100+130)*10-92 = 208.0 >= 200.0
    ctx = LifecycleContext(
        near_pnl_pts=-100.0,
        far_pnl_pts=130.0,
        floating_pnl_pts=130.0,
        entry_age_secs=200.0,
        release_stop_threshold=80.0,
        trail_dist=50.0,
        trailing_side=Side.LONG,
        peak=43500.0,
        nadir=43000.0,
        rem_high=43500.0,
        rem_low=43480.0,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=200.0,
        combined_upl_giveback_twd=50.0,
        combined_upl_exit_mode="fixed_target",
        peak_net_exit_pnl_twd=0.0,
    )
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is not None
    assert decision.action == LifecycleAction.TRAIL
    assert decision.winner == PolicyJTriggerPath.FIXED_TARGET.value


def test_policy_j_explicit_giveback_mode_unchanged():
    """Explicitly setting combined_upl_exit_mode='giveback' must reproduce
    the pre-existing peak-then-retrace behaviour exactly (regression guard
    for the default/back-compat path)."""
    from strategies.plugins.futures.active.mts_lifecycle_adapter import (
        LifecycleContext,
        evaluate_lifecycle_actions,
        ReleaseGroup,
        PolicyJTriggerPath,
    )

    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    ctx = LifecycleContext(
        near_pnl_pts=10.0,
        far_pnl_pts=10.0,
        floating_pnl_pts=20.0,
        entry_age_secs=100.0,
        release_stop_threshold=80.0,
        trail_dist=48.9,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=300.0,
        combined_upl_giveback_twd=100.0,
        combined_upl_exit_mode="giveback",
        peak_net_exit_pnl_twd=400.0,
    )
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is not None
    assert decision.action == LifecycleAction.COMBINED_EXIT
    assert decision.winner == PolicyJTriggerPath.SPREAD_GIVEBACK.value

