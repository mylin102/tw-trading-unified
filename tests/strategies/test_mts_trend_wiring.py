"""MTS 2.0 production wiring tests (2026-08-22).

Proves the trend-release seam:
  1. enabled + valid immutable TrendDecision snapshot reaches TREND_RELEASE
  2. missing snapshot / incomplete snapshot / disabled flag -> BLOCK
  3. entry-side -> counter-trend-leg mapping is correct (never PNL)
  4. Policy J / emergency hard stop precedence over TREND_RELEASE is preserved
  5. incomplete-bar snapshot (no asof/freshness metadata) -> fail closed
"""
import os
from datetime import datetime, timedelta

import pytest

from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    ContextBuildStatus,
    LifecycleAction,
    LifecycleDecision,
    LifecycleEvaluationInput,
    LifecycleEvaluationResult,
    MtsLifecycleAdapter,
    PositionLifecycle,
    counter_trend_leg_from_sides,
)
from strategies.plugins.futures.active.tmf_spread import (
    Leg,
    PositionPhase,
    ReleaseGroupStatus,
    Side,
    TrailGroupStatus,
)


def _valid_snapshot(**over):
    snap = {
        "decision_ts": "2026-07-20T10:00:00",
        "asof_ts": "2026-07-20T10:00:00",
        "direction": "BULLISH",
        "confidence": 0.90,
        "pass_release": True,
        "decision_max_quote_age_ms": 100.0,
        "window_max_quote_age_ms": 500.0,
        "renko": {"source": "renko", "direction": "BULLISH", "score": 1.0},
        "adl": {"source": "adl", "direction": "BULLISH", "score": 1.0},
        "vwap": {"source": "vwap", "direction": "BULLISH", "score": 1.0},
    }
    snap.update(over)
    return snap


def _spread_lifecycle(rg_status=ReleaseGroupStatus.ARMED):
    lc = PositionLifecycle(phase=PositionPhase.SPREAD)
    lc.release_group.status = rg_status
    return lc


def _input(state, lifecycle, mode="LIVE"):
    return LifecycleEvaluationInput(
        strategy_state=state,
        market_event={"event_time": "2026-07-20T10:00:00", "ts": "2026-07-20T10:00:00"},
        lifecycle=lifecycle,
        execution_mode=mode,
    )


# ── 1. enabled + valid snapshot reaches TREND_RELEASE through build_context ──
def test_valid_snapshot_surfaces_in_context_and_candidate_fires():
    adapter = MtsLifecycleAdapter()
    lc = _spread_lifecycle()
    state = {
        "near_pnl_pts": -5.0, "far_pnl_pts": 5.0, "floating_pnl_pts": 0.0,
        "entry_age_secs": 600.0, "release_stop_threshold": 88.0, "trail_dist": 20.0,
        "trend_release_enabled": True,
        "trend_confirmed": _valid_snapshot(),
    }
    ctx, diag = adapter.build_context(_input(state, lc))
    assert ctx is not None
    assert ctx.trend_release_enabled is True
    assert ctx.trend_confirmed is not None
    # the adapter passes the snapshot through as-is (release_leg is injected by
    # the strategy seam from entry sides, never here).
    result: LifecycleEvaluationResult = adapter.evaluate(_input(state, lc))
    # without release_leg the candidate blocks (needs NEAR/FAR); with it, fires.
    assert result.decision is None or result.decision.action != LifecycleAction.TREND_RELEASE


def test_valid_snapshot_with_release_leg_fires_trend_release():
    adapter = MtsLifecycleAdapter()
    lc = _spread_lifecycle()
    snap = _valid_snapshot(release_leg="FAR")
    state = {
        "near_pnl_pts": -5.0, "far_pnl_pts": 5.0, "floating_pnl_pts": 0.0,
        "entry_age_secs": 600.0, "release_stop_threshold": 88.0, "trail_dist": 20.0,
        "trend_release_enabled": True,
        "trend_confirmed": snap,
    }
    result = adapter.evaluate(_input(state, lc))
    assert result.decision is not None
    assert result.decision.action == LifecycleAction.TREND_RELEASE
    assert result.decision.release_leg == Leg.FAR
    assert result.decision.winner == "TREND_CONFIRMED_RELEASE"


# ── 2. missing snapshot / disabled / incomplete -> BLOCK ──
def test_disabled_flag_blocks():
    adapter = MtsLifecycleAdapter()
    lc = _spread_lifecycle()
    state = {"near_pnl_pts": 1.0, "far_pnl_pts": 1.0, "floating_pnl_pts": 2.0,
             "entry_age_secs": 60.0, "release_stop_threshold": 88.0, "trail_dist": 20.0,
             "trend_release_enabled": False, "trend_confirmed": _valid_snapshot(release_leg="FAR")}
    result = adapter.evaluate(_input(state, lc))
    assert result.decision is None or result.decision.action != LifecycleAction.TREND_RELEASE


def test_missing_snapshot_blocks():
    adapter = MtsLifecycleAdapter()
    lc = _spread_lifecycle()
    state = {"near_pnl_pts": 1.0, "far_pnl_pts": 1.0, "floating_pnl_pts": 2.0,
             "entry_age_secs": 60.0, "release_stop_threshold": 88.0, "trail_dist": 20.0,
             "trend_release_enabled": True, "trend_confirmed": None}
    result = adapter.evaluate(_input(state, lc))
    assert result.decision is None or result.decision.action != LifecycleAction.TREND_RELEASE


def test_incomplete_snapshot_blocks():
    adapter = MtsLifecycleAdapter()
    lc = _spread_lifecycle()
    # missing asof_ts / freshness metadata -> adapter drops it (fail closed)
    bad = _valid_snapshot(release_leg="FAR")
    for k in ("asof_ts", "decision_max_quote_age_ms", "window_max_quote_age_ms"):
        bad = dict(bad); bad.pop(k)
        state = {"near_pnl_pts": 1.0, "far_pnl_pts": 1.0, "floating_pnl_pts": 2.0,
                 "entry_age_secs": 60.0, "release_stop_threshold": 88.0, "trail_dist": 20.0,
                 "trend_release_enabled": True, "trend_confirmed": bad}
        result = adapter.evaluate(_input(state, lc))
        assert result.decision is None or result.decision.action != LifecycleAction.TREND_RELEASE


# ── 3. side mapping (pure; never PNL) ──
def test_counter_trend_leg_mapping():
    assert counter_trend_leg_from_sides("LONG", "SHORT") == (Side.LONG, "FAR")
    assert counter_trend_leg_from_sides("SHORT", "LONG") == (Side.SHORT, "NEAR")
    assert counter_trend_leg_from_sides("LONG", "LONG") == (None, None)
    assert counter_trend_leg_from_sides(None, "SHORT") == (None, None)


# ── 4. precedence: Policy J / STOPLOSS beat TREND_RELEASE ──
def test_policy_j_precedes_trend_release():
    adapter = MtsLifecycleAdapter()
    lc = _spread_lifecycle()
    state = {
        "near_pnl_pts": 3.0, "far_pnl_pts": 3.0, "floating_pnl_pts": 6.0,
        "entry_age_secs": 600.0, "release_stop_threshold": 88.0, "trail_dist": 20.0,
        "enable_combined_upl_trail": True,
        "combined_upl_activation_net_pnl_twd": 50.0,
        "combined_upl_giveback_twd": 10.0,
        "peak_net_exit_pnl_twd": 100.0,   # above activation -> giveback fires
        "trend_release_enabled": True,
        "trend_confirmed": _valid_snapshot(release_leg="FAR"),
    }
    result = adapter.evaluate(_input(state, lc))
    assert result.decision is not None
    assert result.decision.action == LifecycleAction.COMBINED_EXIT  # Policy J wins


def test_hard_stop_precedes_trend_release():
    adapter = MtsLifecycleAdapter()
    lc = _spread_lifecycle()
    state = {
        "near_pnl_pts": -5.0, "far_pnl_pts": -5.0, "floating_pnl_pts": -10.0,
        "entry_age_secs": 600.0, "release_stop_threshold": 88.0, "trail_dist": 20.0,
        "max_loss_pts": 8.0,             # floating below -max_loss -> STOPLOSS
        "trend_release_enabled": True,
        "trend_confirmed": _valid_snapshot(release_leg="FAR"),
    }
    result = adapter.evaluate(_input(state, lc))
    assert result.decision is not None
    assert result.decision.action == LifecycleAction.STOPLOSS  # hard stop wins


# ── 5. strategy seam fail-closed (env off / missing snapshot) ──
def test_strategy_seam_fail_closed_when_env_off(monkeypatch):
    monkeypatch.delenv("MTS_TREND_RELEASE_ENABLED", raising=False)
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    s = object.__new__(TMFSpread)
    enabled, snap = s._build_trend_release_input()
    assert enabled is False
    assert snap is None


def test_strategy_seam_fail_closed_without_snapshot(monkeypatch):
    monkeypatch.setenv("MTS_TREND_RELEASE_ENABLED", "1")
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    s = object.__new__(TMFSpread)
    s._near_side = "LONG"; s._far_side = "SHORT"
    s._trend_confirmed_snapshot = None
    enabled, snap = s._build_trend_release_input()
    assert enabled is False
    assert snap is None


def test_strategy_seam_valid_snapshot_enabled(monkeypatch):
    monkeypatch.setenv("MTS_TREND_RELEASE_ENABLED", "1")
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    s = object.__new__(TMFSpread)
    s._near_side = "LONG"; s._far_side = "SHORT"
    s._trend_confirmed_snapshot = _valid_snapshot()
    enabled, snap = s._build_trend_release_input()
    assert enabled is True
    assert snap is not None
    assert snap["release_leg"] == "FAR"
    assert snap["retained_direction"] in ("LONG", "SHORT")