"""
Reproduce test for decision=None bug in evaluate_lifecycle_actions.

Logs from live system show:
    far_hit=True decision=None tick_ct=10544/2 quote_age=0/2000.0

This test isolates evaluate_lifecycle_actions to find the root cause.
"""
from unittest.mock import patch
import logging
import sys
import pytest

from strategies.plugins.futures.active.tmf_spread import (
    PositionLifecycle,
    PositionPhase,
    ReleaseGroup,
    ReleaseGroupStatus,
    TrailGroup,
    TrailGroupStatus,
    LifecycleContext,
    LifecycleDecision,
    LifecycleAction,
    Leg,
    evaluate_lifecycle_actions,
    _check_release_candidates,
)


logging.basicConfig(level=logging.DEBUG, stream=sys.stderr)


# ── Test 1: Direct _check_release_candidates with exact log values ──

def test_check_release_with_exact_log_values():
    """Reproduce the exact log values: near_pnl=270, far_pnl=-208, threshold=158.4."""
    ctx = LifecycleContext(
        near_pnl_pts=270.0,
        far_pnl_pts=-208.0,
        floating_pnl_pts=62.0,  # 270 + (-208) = 62
        entry_age_secs=7200.0,  # 2 hours
        release_stop_threshold=158.4,
        trail_dist=60.0,
    )
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    decisions = _check_release_candidates(ctx, lc)
    assert len(decisions) == 1, f"Expected 1 decision, got {len(decisions)}"
    assert decisions[0].action == LifecycleAction.RELEASE
    assert decisions[0].release_leg == Leg.FAR


# ── Test 2: Full evaluate_lifecycle_actions pipeline ──

def test_evaluate_lifecycle_full_pipeline():
    """Test the full pipeline with exact log values."""
    ctx = LifecycleContext(
        near_pnl_pts=270.0,
        far_pnl_pts=-208.0,
        floating_pnl_pts=62.0,
        entry_age_secs=7200.0,
        release_stop_threshold=158.4,
        trail_dist=60.0,
    )
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is not None, "evaluate_lifecycle_actions returned None — BUG REPRODUCED"
    assert decision.action == LifecycleAction.RELEASE
    assert decision.release_leg == Leg.FAR


# ── Test 3: Full pipeline with current live values ──

def test_evaluate_with_current_live_values():
    """Test using values from the current live position state."""
    ctx = LifecycleContext(
        near_pnl_pts=431.0,      # near profit
        far_pnl_pts=-358.0,      # far loss
        floating_pnl_pts=73.0,   # net PnL
        entry_age_secs=12600.0,  # ~3.5 hours
        release_stop_threshold=133.53,
        trail_dist=60.0,
    )
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is not None, "Current live values also return None"
    assert decision.action == LifecycleAction.RELEASE
    assert decision.release_leg == Leg.FAR


# ── Test 4: What if phase/status are STRINGS (JSON deserialization issue) ──

def test_check_release_with_string_phase():
    """
    If lifecycle is deserialized from JSON with strict=False or 
    via manual dict construction, phase could be a string 'SPREAD'
    instead of PositionPhase.SPREAD. Compare: 'SPREAD' != PositionPhase.SPREAD.
    """
    ctx = LifecycleContext(
        near_pnl_pts=270.0,
        far_pnl_pts=-208.0,
        floating_pnl_pts=62.0,
        entry_age_secs=7200.0,
        release_stop_threshold=158.4,
        trail_dist=60.0,
    )
    # Bad deserialization: phase as string, not enum.
    # 2026-07-16 Gemini CLI: Verified that string values are now successfully supported.
    lc = PositionLifecycle(
        phase="SPREAD",  # type: ignore — deliberate string injection
        release_group=ReleaseGroup(status="ARMED"),  # type: ignore
    )
    decisions = _check_release_candidates(ctx, lc)
    assert len(decisions) == 1, (
        f"Expected 1 decision since string phase/status are now supported, got {len(decisions)}"
    )


# ── Test 5: Evaluate with FLAT lifecycle (wrong state) ──

def test_evaluate_with_flat_lifecycle():
    """If _lifecycle_oca is somehow FLAT, evaluate should return None."""
    ctx = LifecycleContext(
        near_pnl_pts=270.0,
        far_pnl_pts=-208.0,
        floating_pnl_pts=62.0,
        entry_age_secs=7200.0,
        release_stop_threshold=158.4,
        trail_dist=60.0,
    )
    lc = PositionLifecycle()  # defaults to FLAT / INACTIVE
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is None, "FLAT lifecycle should produce no decision"


# ── Test 6: Evaluate with SUBMITTED status (in-flight guard) ──

def test_evaluate_with_submitted_status():
    """If release_group.status is SUBMITTED, evaluate returns None (in-flight guard)."""
    ctx = LifecycleContext(
        near_pnl_pts=270.0,
        far_pnl_pts=-208.0,
        floating_pnl_pts=62.0,
        entry_age_secs=7200.0,
        release_stop_threshold=158.4,
        trail_dist=60.0,
    )
    lc = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.SUBMITTED),
    )
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is None, "SUBMITTED status should block"

    # Also test FILLED
    lc2 = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.FILLED),
    )
    decision2 = evaluate_lifecycle_actions(ctx, lc2)
    assert decision2 is None, "FILLED status should block"


# ── Test 7: Build lifecycle exactly as lifecycle_from_dict does ──

def test_lifecycle_from_dict_style():
    """Reproduce the exact deserialization path used by lifecycle_from_dict."""
    from strategies.plugins.futures.active.tmf_spread import lifecycle_from_dict

    state_release = {
        "status": "ARMED",
        "near_order_id": None, "far_order_id": None,
        "filled_leg": None, "filled_order_id": None,
        "canceled_leg": None, "trigger_ts": None,
        "sibling_cancel_order_id": None, "sibling_cancel_status": None,
        "entry_risk": None,
        "near_price": 45227.7, "far_price": 45864.3,
        "near_side": "sell", "far_side": "buy",
        "order_type": "MKP",
    }
    state_trail = {
        "status": "INACTIVE", "remaining_leg": None,
        "exit_order_id": None, "peak_pnl": None,
        "nadir_pnl": None, "trail_stop": None, "trigger_ts": None,
    }
    lifecycle_dict = {
        "phase": "SPREAD",
        "release_group": state_release,
        "trail_group": state_trail,
    }
    lc = lifecycle_from_dict(lifecycle_dict)
    assert lc.phase == PositionPhase.SPREAD
    assert lc.release_group.status == ReleaseGroupStatus.ARMED

    ctx = LifecycleContext(
        near_pnl_pts=431.0,
        far_pnl_pts=-358.0,
        floating_pnl_pts=73.0,
        entry_age_secs=12600.0,
        release_stop_threshold=133.53,
        trail_dist=60.0,
    )
    decision = evaluate_lifecycle_actions(ctx, lc)
    assert decision is not None, "Deserialized lifecycle still returns None"
    assert decision.action == LifecycleAction.RELEASE
    assert decision.release_leg == Leg.FAR
