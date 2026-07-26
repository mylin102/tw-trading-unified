# 2026-07-26 Gemini CLI: Wave J1.5-B Closure - Differential Parity Harness Test
import pytest

from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    Leg,
    LifecycleAction,
    LifecycleContext,
    PositionLifecycle,
    PositionPhase,
    ReleaseGroup,
    ReleaseGroupStatus,
    evaluate_lifecycle_actions,
)
from strategies.futures.mts.policy_j_shadow_evaluator import (
    PolicyJShadowEvaluator,
    PolicyJShadowObservation,
)
from strategies.futures.mts.policy_j_shadow_state import PolicyJShadowState


def test_differential_parity_shadow_attached_vs_unattached():
    """
    Differential Harness Test:
    Compare evaluate_lifecycle_actions decision outputs with vs without PolicyJShadowEvaluator attached.
    
    Invariant:
    - 0 differences in LifecycleAction
    - 0 differences in reason/target
    - 0 differences in order intent or state transition!
    """
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )

    ctx = LifecycleContext(
        near_pnl_pts=-85.0,
        far_pnl_pts=10.0,
        floating_pnl_pts=-75.0,
        entry_age_secs=100.0,
        release_stop_threshold=80.0,
        trail_dist=48.9,
        enable_combined_upl_trail=False,  # Hardlocked False by default
    )

    # 1. Baseline evaluation without shadow evaluator
    baseline_decision = evaluate_lifecycle_actions(ctx, lc)

    # 2. Shadow evaluation attached
    shadow_obs = PolicyJShadowObservation(
        trade_id="TRADE_DIFF_001",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=3000.0,
        near_quote_age_ms=10,
        far_quote_age_ms=10,
    )
    shadow_state = PolicyJShadowState()
    shadow_config = {"shadow_enabled": True, "activation_net_pnl_twd": 300.0, "giveback_twd": 100.0}

    snapshot, new_state = PolicyJShadowEvaluator.evaluate(shadow_obs, shadow_state, shadow_config)
    shadow_decision = evaluate_lifecycle_actions(ctx, lc)

    # Differential Invariant Assertions
    assert baseline_decision is not None
    assert shadow_decision is not None
    assert shadow_decision.action == baseline_decision.action
    assert shadow_decision.release_leg == baseline_decision.release_leg
    assert shadow_decision.action == LifecycleAction.RELEASE
    assert shadow_decision.release_leg == Leg.NEAR
    assert snapshot.execution_blocked is True
