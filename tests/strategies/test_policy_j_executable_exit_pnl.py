"""2026-11-03 Hermes Agent: the combined profit target must be judged on
EXECUTABLE prices (bid for closing a long, ask for closing a short), not on
the last/mid mark.

Evidence (Mini PAPER 2026-11-02 night): all 7 combined exits were
POLICY_J_FIXED_TARGET with current_net_exit_pnl_twd = +208/+218, but the
actual fills settled at -96/-56/-176/-6/+74/+14/+44 (net -203.6). Mark-to-fill
gap averaged 125 TWD (12.5 pts on the spread) over 4-7s, on top of 116.2 TWD
of real round-trip cost.

These tests drive the real entry points:
  * MtsLifecycleAdapter.evaluate()  -- the target decision
  * TMFSpread._exit_side_pnl()      -- the executable-price computation
"""
from types import SimpleNamespace

import pytest

import strategies.plugins.futures.active.tmf_spread as tmf
from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    LifecycleAction,
    LifecycleEvaluationInput,
    MtsLifecycleAdapter,
    PolicyJTriggerPath,
    PositionLifecycle,
    PositionPhase,
    ReleaseGroupStatus,
)


def _eval(**overrides):
    lifecycle = PositionLifecycle(phase=PositionPhase.SPREAD)
    lifecycle.release_group.status = ReleaseGroupStatus.ARMED
    state = {
        "near_pnl_pts": 20.0, "far_pnl_pts": 20.0, "floating_pnl_pts": 40.0,
        "entry_age_secs": 120.0, "release_stop_threshold": 88.0, "trail_dist": 66.0,
        "enable_combined_upl_trail": True,
        "combined_upl_activation_net_pnl_twd": 200.0,
        "combined_upl_giveback_twd": 50.0,
        "combined_upl_exit_mode": "fixed_target",
        "peak_net_exit_pnl_twd": 0.0,
    }
    state.update(overrides)
    return MtsLifecycleAdapter().evaluate(LifecycleEvaluationInput(
        strategy_state=state, market_event={"event_time": "2026-11-03T09:10:00"},
        lifecycle=lifecycle, execution_mode="PAPER",
    ))


def test_mark_pnl_would_trigger_but_executable_pnl_does_not():
    # mark: (20+20)*10 - 92 = 308 -> above 200 (old behaviour fires)
    base = _eval()
    assert base.decision is not None and base.decision.winner == PolicyJTriggerPath.FIXED_TARGET.value
    # executable: 2 pts worse on each leg -> (18+18)*10 - 92 = 268 ... still above;
    # use the observed 12.5 pt spread gap: 20+20 -> 13.75+13.75 = 27.5 pts
    ex = _eval(near_exit_pnl_pts=13.75, far_exit_pnl_pts=13.75)
    assert ex.decision is None or ex.decision.winner != PolicyJTriggerPath.FIXED_TARGET.value


def test_executable_pnl_still_triggers_when_it_really_clears_target():
    r = _eval(near_exit_pnl_pts=25.0, far_exit_pnl_pts=25.0)   # (50*10)-92 = 408
    assert r.decision is not None
    assert r.decision.winner == PolicyJTriggerPath.FIXED_TARGET.value


def test_giveback_mode_unaffected_by_executable_fields():
    r = _eval(combined_upl_exit_mode="giveback", peak_net_exit_pnl_twd=400.0,
              near_exit_pnl_pts=1.0, far_exit_pnl_pts=1.0)
    # peak 400, giveback 50 -> triggers on the peak path regardless
    assert r.decision is not None


@pytest.fixture
def _strategy():
    s = object.__new__(tmf.TMFSpread)
    s._near_entry = 1000.0
    s._far_entry = 1100.0
    s._near_side = "LONG"
    s._far_side = "SHORT"
    return s


def test_long_leg_valued_at_bid_short_leg_at_ask(_strategy):
    bar = {"near_bid": 1010.0, "near_ask": 1011.0, "far_bid": 1089.0, "far_ask": 1090.0}
    n, f, basis = _strategy._exit_side_pnl(bar, 1010.5, 1089.5)
    assert basis == "EXIT_SIDE_BBO"
    assert n == pytest.approx(10.0)      # LONG closes at bid 1010
    assert f == pytest.approx(10.0)      # SHORT closes at ask 1090


def test_missing_bbo_falls_back_to_mark_and_says_so(_strategy):
    n, f, basis = _strategy._exit_side_pnl({}, 1005.0, 1095.0)
    assert basis == "MARK_FALLBACK"
    assert n == pytest.approx(5.0)
    assert f == pytest.approx(5.0)


def test_crossed_or_invalid_bbo_falls_back(_strategy):
    n, f, basis = _strategy._exit_side_pnl(
        {"near_bid": 1012.0, "near_ask": 1010.0, "far_bid": -1, "far_ask": 1090.0},
        1005.0, 1095.0)
    assert basis == "MARK_FALLBACK"
    assert (n, f) == (5.0, 5.0)
