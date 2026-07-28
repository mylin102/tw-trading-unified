"""Regression tests: remaining-leg trail must respect config atr_multiplier_trail.

2026-07-28: PR 6.6B — remove unvalidated MFE adaptive trail widening.
mfe_tighten introduced at 8a4897ae (AI-generated, no ADR/tests/replay)
overrode atr_multiplier_trail with 1.2-1.6× ATR after MFE >= 2-3 ATR.

Rollback target:  MFE-based multiplier override
Retained:         Config-driven remaining-leg trailing stop (atr_multiplier_trail)
Not allowed:      Hard-coded ATR multiplier
"""
import pytest


def _make_strategy(atr_mult_trail: float = 0.2,
                   atr_mult_stop: float = 0.8):
    """Construct a minimal TMFSpread instance for threshold testing."""
    import pandas as pd  # noqa: F401 — tmf_spread imports it at module level
    from strategies.plugins.futures.active.tmf_spread import TMFSpread

    strategy = TMFSpread.__new__(TMFSpread)
    strategy._atr_mult_trail = atr_mult_trail
    strategy._atr_mult_stop = atr_mult_stop
    strategy._params = {}
    strategy._last_atr = 100.0
    strategy._release_stop_fixed = 88.0
    strategy._trail_dist_fixed = 60.0
    strategy._mfe_pts = 0.0
    strategy._atr_cap = 0.0
    return strategy


@pytest.mark.parametrize("configured_mult", [0.2, 0.5, 0.8])
@pytest.mark.parametrize("mfe_atr", [0.0, 2.0, 3.0, 10.0])
def test_remaining_leg_trail_always_uses_configured_multiplier(
    configured_mult: float, mfe_atr: float,
):
    """Remaining-leg trail must equal atr * configured atr_multiplier_trail,
    regardless of MFE level (no mfe_tighten override)."""
    atr = 100.0
    strategy = _make_strategy(atr_mult_trail=configured_mult)
    strategy._mfe_pts = mfe_atr * atr

    _, trail = strategy._get_thresholds({"atr": atr})

    assert trail == pytest.approx(atr * configured_mult, rel=0.01), \
        f"configured_mult={configured_mult} mfe_atr={mfe_atr}: " \
        f"expected trail={atr * configured_mult:.2f}, got {trail:.2f}"


@pytest.mark.parametrize("configured_mult", [0.2, 0.5, 0.8])
def test_stop_unaffected_by_trail_mult(configured_mult: float):
    """Release stop must remain atr * atr_mult_stop regardless of trail_mult."""
    atr = 100.0
    strategy = _make_strategy(atr_mult_trail=configured_mult)
    strategy._atr_mult_stop = 0.8

    stop, _ = strategy._get_thresholds({"atr": atr})

    assert stop == pytest.approx(80.0, rel=0.01), \
        f"expected stop=80.0, got {stop:.2f} (configured_mult={configured_mult})"
