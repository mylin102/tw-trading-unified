import os
import pytest
from datetime import datetime
from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    LifecycleContext, PositionLifecycle, PositionPhase, ReleaseGroup,
    ReleaseGroupStatus, TrailGroup, TrailGroupStatus, LifecycleAction,
    evaluate_lifecycle_actions,
)
from strategies.plugins.futures.active.tmf_spread import Side, Leg

def test_policy_j_first_trigger_preempts_same_tick_release():
    ctx = LifecycleContext(
        near_pnl_pts=10.0,
        far_pnl_pts=10.0,
        floating_pnl_pts=20.0,
        entry_age_secs=100.0,
        trail_dist=20.0,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=150.0,
        combined_upl_giveback_twd=50.0,
        peak_net_exit_pnl_twd=200.0,
        release_stop_threshold=5.0,
    )
    lifecycle = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    decision = evaluate_lifecycle_actions(ctx, lifecycle)
    assert decision is not None
    assert decision.action == LifecycleAction.COMBINED_EXIT

def test_policy_j_triggered_state_blocks_single_leg_without_retriggering():
    ctx = LifecycleContext(
        near_pnl_pts=10.0,
        far_pnl_pts=10.0,
        floating_pnl_pts=20.0,
        entry_age_secs=100.0,
        trail_dist=20.0,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=150.0,
        combined_upl_giveback_twd=50.0,
        peak_net_exit_pnl_twd=200.0,
        release_stop_threshold=5.0,
    )
    lifecycle = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.TRIGGERED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
        exit_owner="POLICY_J",
    )
    decision = evaluate_lifecycle_actions(ctx, lifecycle)
    assert decision is None
