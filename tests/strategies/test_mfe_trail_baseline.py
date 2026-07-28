"""Regression tests: MFE trail baseline and mfe_tighten disablement.

2026-07-28: PR 6.6B — rollback unvalidated MFE adaptive trail widening.
mfe_tighten introduced at commit 8a4897ae without ADR, tests, or replay.
Production trail must default to base atr_multiplier_trail (0.2 × ATR).
"""
import pytest


def test_default_trail_uses_base_atr_multiplier():
    """With mfe_tighten disabled, trail must be atr * base_mult (0.2)."""
    import pandas as pd
    from strategies.plugins.futures.active.tmf_spread import TMFSpread

    strategy = TMFSpread.__new__(TMFSpread)
    strategy._atr_mult_trail = 0.2
    strategy._atr_mult_stop = 0.8
    strategy._params = {"mfe_tighten": {"enabled": False}}
    strategy._last_atr = 113.9
    strategy._release_stop_fixed = 88.0
    strategy._trail_dist_fixed = 60.0

    bar = {"atr": 113.9}
    stop, trail = strategy._get_thresholds(bar)

    expected_trail = 113.9 * 0.2  # = 22.78
    assert trail == pytest.approx(expected_trail, rel=0.01), \
        f"Expected trail={expected_trail:.2f}, got {trail:.2f}"
    assert stop == pytest.approx(113.9 * 0.8, rel=0.01)


def test_disabled_mfe_adaptation_cannot_change_trail():
    """MFE adaptation must not affect trail when enabled=false."""
    import pandas as pd
    from strategies.plugins.futures.active.tmf_spread import TMFSpread

    strategy = TMFSpread.__new__(TMFSpread)
    strategy._atr_mult_trail = 0.2
    strategy._atr_mult_stop = 0.8
    strategy._params = {"mfe_tighten": {"enabled": False}}
    strategy._last_atr = 113.9
    strategy._release_stop_fixed = 88.0
    strategy._trail_dist_fixed = 60.0

    for mfe_pts in [0, 2 * 113.9, 3 * 113.9, 10 * 113.9]:
        strategy._mfe_pts = mfe_pts
        _, trail = strategy._get_thresholds({"atr": 113.9})
        assert trail == pytest.approx(22.78, rel=0.01), \
            f"MFE={mfe_pts:.0f} changed trail to {trail:.2f} (expected 22.78)"
