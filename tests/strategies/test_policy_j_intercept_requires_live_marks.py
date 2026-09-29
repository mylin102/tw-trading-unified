"""S3: the Policy J release-stop INTERCEPT must not fire on a stale bar mark.

Live 2026-09-29 09:01:42 (trade broker-reconciled-b44e20763a980f92): the decision
marks came from the last CLOSED 5m bar (near 47931, one bar old) while the live
near was 48050/48052 and the exit filled at 48063.  The near leg's profit was
therefore overstated by ~132 points and Policy J intercepted the far-leg
release with a phantom "+126 points" combined exit:

    [POLICY_J_RELEASE_INTERCEPT] Single leg hit release stop
      (near_hit=False far_hit=True) but Total Net PnL (1168.0 TWD) >=
      Activation (200.0 TWD) -> Intercept with COMBINED_EXIT

(1168.0 = (245 + -119) * 10 - 92 friction, i.e. computed from the stale mark.)

Contract: a RISK release (single-leg stop) may still act on whatever mark it
has, but the INTERCEPT is an optimisation - it must never be chosen on marks
known to be the last closed bar.  When the interceptor declines, the ordinary
release candidate wins by priority, so risk control is not weakened.
"""
from types import SimpleNamespace

import pytest

from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    LifecycleAction,
    LifecycleContext,
    PolicyJTriggerPath,
    PositionPhase,
    ReleaseGroupStatus,
    _check_combined_upl_trail_candidate,
)

_UNSET = object()

# Incident numbers (09:01:42.298).
_INCIDENT = dict(
    near_pnl_pts=245.0,
    far_pnl_pts=-119.0,
    floating_pnl_pts=126.0,
    entry_age_secs=600.0,
    release_stop_threshold=115.17,
    trail_dist=66.0,
    enable_combined_upl_trail=True,
    combined_upl_activation_net_pnl_twd=200.0,
    combined_upl_giveback_twd=50.0,
    peak_net_exit_pnl_twd=0.0,
)


def _lifecycle():
    return SimpleNamespace(
        phase=PositionPhase.SPREAD,
        release_group=SimpleNamespace(status=ReleaseGroupStatus.ARMED),
        exit_owner=None,
    )


def _ctx(marks_live=_UNSET):
    kwargs = dict(_INCIDENT)
    if marks_live is not _UNSET:
        kwargs["marks_live"] = marks_live
    return LifecycleContext(**kwargs)


def _winners(decisions):
    return [d.winner for d in decisions]


def test_intercept_is_refused_when_the_marks_are_a_closed_bar():
    """RED at base: the incident replay still intercepts on stale marks."""
    decisions = _check_combined_upl_trail_candidate(_ctx(marks_live=False),
                                                    _lifecycle())
    assert decisions == []


def test_intercept_still_fires_on_live_marks():
    """Control: with live marks the rule behaves exactly as before."""
    decisions = _check_combined_upl_trail_candidate(_ctx(marks_live=True),
                                                    _lifecycle())
    assert len(decisions) == 1
    assert decisions[0].action is LifecycleAction.COMBINED_EXIT
    assert decisions[0].winner == PolicyJTriggerPath.RELEASE_INTERCEPT.value


def test_unknown_mark_freshness_keeps_the_existing_behaviour():
    """Backward compatibility: an unset flag (paper/backtest/replay) is not a
    stale proof, so nothing changes for existing callers."""
    decisions = _check_combined_upl_trail_candidate(_ctx(), _lifecycle())
    assert len(decisions) == 1
    assert decisions[0].action is LifecycleAction.COMBINED_EXIT


@pytest.mark.parametrize("marks_live,expected", [
    (False, False),
    (True, True),
])
def test_dataclass_field_default_is_permissive(marks_live, expected):
    """The new field must default permissively (True) and be overridable."""
    ctx = LifecycleContext(**{**_INCIDENT, "marks_live": marks_live})
    assert bool(ctx.marks_live) is expected



# ── real-path wiring (build_context), added after the A/B caught the gap ───────

def test_build_context_reads_marks_live_from_the_strategy_state():
    """The shipped path: the adapter must carry the flag it is given."""
    from datetime import datetime

    from strategies.plugins.futures.active.mts_lifecycle_adapter import (
        ContextBuildStatus,
        LifecycleEvaluationInput,
        MtsLifecycleAdapter,
    )

    adapter = MtsLifecycleAdapter()
    lifecycle = _lifecycle()
    base_time = datetime(2026, 9, 29, 9, 1, 42)
    state = {
        "last_applied_event_time": base_time.isoformat(),
        "marks_live": False,
    }
    ctx, diag = adapter.build_context(LifecycleEvaluationInput(
        strategy_state=state,
        market_event={"event_time": base_time.isoformat()},
        lifecycle=lifecycle,
        execution_mode="BACKTEST",
    ))

    assert diag.build_status == ContextBuildStatus.VALID
    assert ctx is not None
    assert ctx.marks_live is False


def test_build_context_defaults_marks_live_true_when_absent():
    """Backward compatibility on the shipped path."""
    from datetime import datetime

    from strategies.plugins.futures.active.mts_lifecycle_adapter import (
        LifecycleEvaluationInput,
        MtsLifecycleAdapter,
    )

    adapter = MtsLifecycleAdapter()
    base_time = datetime(2026, 9, 29, 9, 1, 42)
    ctx, _diag = adapter.build_context(LifecycleEvaluationInput(
        strategy_state={"last_applied_event_time": base_time.isoformat()},
        market_event={"event_time": base_time.isoformat()},
        lifecycle=_lifecycle(),
        execution_mode="BACKTEST",
    ))

    assert ctx is not None
    assert ctx.marks_live is True
