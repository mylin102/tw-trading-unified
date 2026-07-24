# 2026-07-25 Gemini CLI: Unit tests proving _evaluate_risk() decision parity and typed RecoveryState enum comparison
import pytest
from strategies.plugins.futures.active.tmf_spread import RecoveryState, infer_lifecycle_from_legacy_state
from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    MtsLifecycleAdapter,
    LifecycleEvaluationInput,
    LifecycleAction,
    Leg,
    PositionPhase,
    ReleaseGroupStatus,
)

def test_evaluate_risk_decision_parity_for_301_loss():
    """Prove _evaluate_risk / MtsLifecycleAdapter evaluates -301 PnL vs 117.42 stop as RELEASE NEAR."""
    adapter = MtsLifecycleAdapter()
    
    # Construct input matching the incident scenario: Near PnL = -301.0, Far PnL = +302.0, Release Stop = 117.42
    lc = infer_lifecycle_from_legacy_state({"has_position": True, "release_state": "BOTH_HELD"})
    
    eval_input = LifecycleEvaluationInput(
        strategy_state={
            "near_pnl_pts": -301.0,
            "far_pnl_pts": 302.0,
            "floating_pnl_pts": 1.0,
            "entry_age_secs": 120.0,
            "release_stop_threshold": 117.42,
            "trail_dist": 48.92,
            "manual_requested": False,
        },
        market_event={"event_time": "2026-07-25T03:04:00", "timestamp": "2026-07-25T03:04:00"},
        lifecycle=lc,
        execution_mode="LIVE"
    )
    
    res = adapter.evaluate(eval_input)
    assert res.decision is not None
    assert res.decision.action == LifecycleAction.RELEASE
    assert res.decision.release_leg == Leg.NEAR

def test_recovery_state_typed_enum_comparison():
    """Prove typed RecoveryState enum comparison handles RecoveryState enums and strings safely."""
    active_states = (RecoveryState.RECOVERED, RecoveryState.FLAT_CONFIRMED)
    
    # Enum instances
    s1 = RecoveryState.RECOVERED
    s2 = RecoveryState.FLAT_CONFIRMED
    s3 = RecoveryState.SPLIT_BRAIN
    
    assert s1 in active_states
    assert s2 in active_states
    assert s3 not in active_states
    
    # String equivalence
    assert s1 == "RECOVERED"
    assert s2 == "FLAT_CONFIRMED"
