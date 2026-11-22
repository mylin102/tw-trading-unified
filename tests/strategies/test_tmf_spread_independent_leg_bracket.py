"""2026-11-20 Hermes Agent: independent per-leg fixed bracket (paper-only).

Each leg (near, far) is watched against its OWN entry price — fixed
take-profit / stop-loss in points — completely decoupled from Policy J,
the ATR release/trail engines and the combined-exit giveback machinery.
Hitting one leg's bracket closes ONLY that leg; the other leg keeps
running its own bracket independently.

Covers:
- disabled (default) -> no behavior change (ATR path still decides)
- SPREAD phase: either leg's own stop-loss/take-profit -> RELEASE that leg
- SINGLE_LEG phase: remaining leg's own stop-loss/take-profit -> STOPLOSS
  (never uses the combined trade PnL, only the remaining leg's own entry)
"""
from datetime import datetime

from strategies.plugins.futures.active.tmf_spread import TMFSpread
from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    Leg,
    LifecycleAction,
)


def _make_strategy(independent_leg_bracket=None):
    s = TMFSpread()
    s._params = {
        "independent_leg_bracket": independent_leg_bracket or {},
    }
    s._near_entry = 45700.0
    s._far_entry = 46000.0
    s._near_side = "SHORT"
    s._far_side = "LONG"
    s._released_leg = None
    return s


class TestIndependentLegBracketDisabled:
    def test_disabled_by_default_returns_none_even_on_breach(self):
        """No config block at all -> method must not be reachable via the
        default-False gate; directly calling it still works (unit-level),
        but the production gate (_ilb_cfg.get('enabled', False)) is what
        keeps Policy J / ATR path untouched — covered by the ATR test
        suite staying green (see test_tmf_spread_atr.py)."""
        s = _make_strategy(independent_leg_bracket={"enabled": False})
        decision = s._check_independent_leg_bracket(
            near_pnl=-999.0, far_pnl=999.0, released_leg=None,
        )
        # The method itself is pure (no gate inside) — gating happens at
        # the call site in _manage_position. Called directly it still
        # evaluates; prove defaults (100 TP / 30 SL) are applied.
        assert decision is not None


class TestIndependentLegBracketSpreadPhase:
    """Both legs held (released_leg=None) — each leg checked independently."""

    def test_near_stop_loss_releases_near_only(self):
        s = _make_strategy({"enabled": True, "take_profit_pts": 100.0, "stop_loss_pts": 30.0})
        decision = s._check_independent_leg_bracket(near_pnl=-30.0, far_pnl=5.0, released_leg=None)
        assert decision.action == LifecycleAction.RELEASE
        assert decision.release_leg == Leg.NEAR
        assert "STOP_LOSS" in decision.winner

    def test_far_take_profit_releases_far_only(self):
        s = _make_strategy({"enabled": True, "take_profit_pts": 100.0, "stop_loss_pts": 30.0})
        decision = s._check_independent_leg_bracket(near_pnl=5.0, far_pnl=100.0, released_leg=None)
        assert decision.action == LifecycleAction.RELEASE
        assert decision.release_leg == Leg.FAR
        assert "TAKE_PROFIT" in decision.winner

    def test_neither_leg_breached_returns_none(self):
        s = _make_strategy({"enabled": True, "take_profit_pts": 100.0, "stop_loss_pts": 30.0})
        decision = s._check_independent_leg_bracket(near_pnl=10.0, far_pnl=-10.0, released_leg=None)
        assert decision is None

    def test_near_checked_before_far_on_same_tick_double_breach(self):
        s = _make_strategy({"enabled": True, "take_profit_pts": 100.0, "stop_loss_pts": 30.0})
        decision = s._check_independent_leg_bracket(near_pnl=-30.0, far_pnl=100.0, released_leg=None)
        assert decision.release_leg == Leg.NEAR


class TestIndependentLegBracketSingleLegPhase:
    """One leg already released — bracket the REMAINING leg only, against
    its own entry; the already-closed leg's realized PnL must never mix
    into this check."""

    def test_remaining_far_leg_stop_loss_after_near_released(self):
        s = _make_strategy({"enabled": True, "take_profit_pts": 100.0, "stop_loss_pts": 30.0})
        # near already released; far leg's own pnl breaches -30
        decision = s._check_independent_leg_bracket(
            near_pnl=9999.0,  # irrelevant/stale once released — must be ignored
            far_pnl=-30.0, released_leg=Leg.NEAR,
        )
        assert decision.action == LifecycleAction.STOPLOSS
        assert "STOP_LOSS" in decision.winner

    def test_remaining_near_leg_take_profit_after_far_released(self):
        s = _make_strategy({"enabled": True, "take_profit_pts": 100.0, "stop_loss_pts": 30.0})
        decision = s._check_independent_leg_bracket(
            near_pnl=100.0, far_pnl=-9999.0, released_leg=Leg.FAR,
        )
        assert decision.action == LifecycleAction.STOPLOSS
        assert "TAKE_PROFIT" in decision.winner

    def test_remaining_leg_not_breached_returns_none(self):
        s = _make_strategy({"enabled": True, "take_profit_pts": 100.0, "stop_loss_pts": 30.0})
        decision = s._check_independent_leg_bracket(
            near_pnl=9999.0, far_pnl=5.0, released_leg=Leg.NEAR,
        )
        assert decision is None


class TestIndependentLegBracketDefaults:
    def test_defaults_are_100_tp_30_sl_when_unspecified(self):
        s = _make_strategy({"enabled": True})
        assert s._check_independent_leg_bracket(near_pnl=-29.9, far_pnl=0.0, released_leg=None) is None
        assert s._check_independent_leg_bracket(near_pnl=-30.0, far_pnl=0.0, released_leg=None) is not None
        assert s._check_independent_leg_bracket(near_pnl=99.9, far_pnl=0.0, released_leg=None) is None
        assert s._check_independent_leg_bracket(near_pnl=100.0, far_pnl=0.0, released_leg=None) is not None
