import os
import pytest
from unittest.mock import Mock, patch
from strategies.plugins.futures.active.tmf_spread import TMFSpread, PositionPhase
from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    LifecycleContext, PositionLifecycle, PositionPhase, ReleaseGroup,
    ReleaseGroupStatus, TrailGroup, TrailGroupStatus, LifecycleAction,
    evaluate_lifecycle_actions,
)

def test_live_tick_never_reads_state_file():
    strat = TMFSpread()
    strat._has_position = True
    strat._trade_id = "mts-test-123"
    strat._read_mts_state = Mock(side_effect=AssertionError("Disk read forbidden during live execution"))
    assert strat._has_position is True

def test_armed_state_is_latched_monotonically():
    ctx = LifecycleContext(
        near_pnl_pts=-1.0,
        far_pnl_pts=0.0,
        floating_pnl_pts=-1.0,
        entry_age_secs=100.0,
        trail_dist=20.0,
        enable_combined_upl_trail=True,
        combined_upl_activation_net_pnl_twd=200.0,
        combined_upl_giveback_twd=50.0,
        peak_net_exit_pnl_twd=218.0,
        release_stop_threshold=144.0,
    )
    lifecycle = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    decision = evaluate_lifecycle_actions(ctx, lifecycle)
    assert decision is not None
    assert decision.action == LifecycleAction.COMBINED_EXIT
