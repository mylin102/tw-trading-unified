"""2026-10-14 Hermes Agent: keep hedged pairs intact during the day open."""

import pytest

from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    LifecycleAction,
    LifecycleEvaluationInput,
    MtsLifecycleAdapter,
    PositionLifecycle,
    PositionPhase,
    ReleaseGroupStatus,
    TrailGroupStatus,
)


def _evaluate(event_time, release_action, **overrides):
    lifecycle = PositionLifecycle(phase=PositionPhase.SPREAD)
    lifecycle.release_group.status = ReleaseGroupStatus.ARMED
    state = {
        "near_pnl_pts": 156.0, "far_pnl_pts": -148.0,
        "floating_pnl_pts": 8.0, "entry_age_secs": 600.0,
        "release_stop_threshold": 141.93, "trail_dist": 66.0,
        "release_trend_gate_enabled": True,
        "trend_release_enabled": release_action == LifecycleAction.TREND_RELEASE,
        "trend_confirmed": {
            "decision_ts": event_time, "asof_ts": event_time,
            "direction": "BULLISH", "confidence": 0.9,
            "pass_release": True, "release_leg": "FAR",
            "decision_max_quote_age_ms": 100.0,
            "window_max_quote_age_ms": 500.0,
        },
    }
    state.update(overrides)
    result = MtsLifecycleAdapter().evaluate(LifecycleEvaluationInput(
        strategy_state=state, market_event={"event_time": event_time},
        lifecycle=lifecycle, execution_mode="PAPER",
    ))
    return result, lifecycle


@pytest.mark.parametrize("release_action", [
    LifecycleAction.RELEASE, LifecycleAction.TREND_RELEASE,
])
@pytest.mark.parametrize("event_time,blocked", [
    ("2026-10-14T08:44:59.999999", False),
    ("2026-10-14T08:45:00", True),
    ("2026-10-14T08:59:59.999999", True),
    ("2026-10-14T09:00:00", False),
    ("2026-10-14T15:00:00", False),
    ("2026-10-15T00:50:00", False),
    ("2026-10-14T08:50:00+08:00", True),
    ("2026-10-14T00:50:00+00:00", True),
    ("2026-10-14T01:00:00+00:00", False),
    ("2026-10-14T08:50:00+00:00", False),
])
def test_opening_window_blocks_both_release_paths(event_time, blocked, release_action):
    result, lifecycle = _evaluate(event_time, release_action)
    assert result.context is not None
    if blocked:
        assert result.decision is None
    else:
        assert result.decision is not None
        assert result.decision.action == release_action
    assert lifecycle.phase == PositionPhase.SPREAD
    assert lifecycle.release_group.status == ReleaseGroupStatus.ARMED


def test_opening_window_blocks_atr_even_when_trend_gate_is_disabled():
    result, _ = _evaluate(
        "2026-10-14T08:50:00", LifecycleAction.RELEASE,
        release_trend_gate_enabled=False, trend_confirmed=None,
    )
    assert result.decision is None


@pytest.mark.parametrize("overrides,expected", [
    ({"manual_requested": True}, LifecycleAction.MANUAL),
    ({"max_loss_pts": 300.0, "floating_pnl_pts": -304.0}, LifecycleAction.STOPLOSS),
    ({"max_hold_secs": 300.0}, LifecycleAction.TIMEOUT),
    ({"enable_combined_upl_trail": True,
      "combined_upl_activation_net_pnl_twd": 300.0,
      "combined_upl_giveback_twd": 100.0,
      "peak_net_exit_pnl_twd": 400.0}, LifecycleAction.COMBINED_EXIT),
])
def test_opening_window_preserves_whole_pair_exits(overrides, expected):
    result, _ = _evaluate("2026-10-14T08:50:00", LifecycleAction.TREND_RELEASE, **overrides)
    assert result.decision is not None
    assert result.decision.action == expected


def test_opening_window_preserves_remaining_leg_trailing_exit():
    lifecycle = PositionLifecycle(phase=PositionPhase.SINGLE_LEG)
    lifecycle.release_group.status = ReleaseGroupStatus.COMPLETED
    lifecycle.trail_group.status = TrailGroupStatus.ARMED
    result = MtsLifecycleAdapter().evaluate(LifecycleEvaluationInput(
        strategy_state={
            "single_leg_started_at": "2026-10-14T08:44:00",
            "trailing_side": "LONG", "peak": 49000.0, "nadir": 48900.0,
            "rem_high": 48990.0, "rem_low": 48930.0, "trail_dist": 66.0,
        },
        market_event={"event_time": "2026-10-14T08:50:00"},
        lifecycle=lifecycle, execution_mode="PAPER",
    ))
    assert result.decision is not None
    assert result.decision.action == LifecycleAction.TRAIL
