"""Regression tests: remaining-leg trail uses config-driven floors.

Authority chain:
  effective_trail = max(configured_trail_distance_points, ATR × atr_multiplier_trail)
  effective_stop  = max(configured_release_stop_points,  ATR × atr_multiplier_stop)

Both _get_thresholds (runtime) and _get_risk_meta (state file) share
_calculate_effective_thresholds() — no hardcoded floor divergence.
"""
import pytest


def _make_strategy(atr_mult_trail: float = 0.5,
                   atr_mult_stop: float = 0.8,
                   trail_fixed: float = 60.0,
                   stop_fixed: float = 88.0,
                   atr_cap: float = 250.0):
    """Construct a minimal TMFSpread instance for threshold testing."""
    import pandas as pd  # noqa: F401
    from strategies.plugins.futures.active.tmf_spread import TMFSpread

    strategy = TMFSpread.__new__(TMFSpread)
    strategy._atr_mult_trail = atr_mult_trail
    strategy._atr_mult_stop = atr_mult_stop
    strategy._trail_dist_fixed = trail_fixed
    strategy._release_stop_fixed = stop_fixed
    strategy._params = {}
    strategy._last_atr = 100.0
    strategy._atr_cap = atr_cap
    strategy._mfe_pts = 0.0
    strategy._side = None
    return strategy


@pytest.mark.parametrize(
    "atr, mult, trail_fixed, expected_trail",
    [
        (40.0,   0.5, 60.0,  60.0),   # ATR trail = 20 → floor 60
        (115.5,  0.5, 60.0,  60.0),   # ATR trail = 57.75 → floor 60
        (150.0,  0.5, 60.0,  75.0),   # ATR trail = 75 → above floor
        (300.0,  0.5, 60.0, 125.0),   # ATR trail = 150 → capped at 250 → 125
        (40.0,   0.5, 30.0,  30.0),   # floor = 30, ATR trail = 20 → floor 30
        (100.0,  0.2, 60.0,  60.0),   # floor = 60, ATR trail = 20 → floor 60
        (200.0,  0.2, 60.0,  60.0),   # floor = 60, ATR trail = 40 → floor 60
    ],
)
def test_runtime_trail_uses_configured_floor(
    atr: float, mult: float, trail_fixed: float, expected_trail: float,
):
    strategy = _make_strategy(atr_mult_trail=mult, trail_fixed=trail_fixed)

    _, trail = strategy._get_thresholds({"atr": atr})

    assert trail == pytest.approx(expected_trail, rel=0.01), \
        f"ATR={atr} mult={mult} floor={trail_fixed}: " \
        f"expected {expected_trail}, got {trail:.2f}"


@pytest.mark.parametrize(
    "atr, mult, stop_fixed, expected_stop",
    [
        (40.0,   0.8, 88.0,  88.0),   # ATR stop = 32 → floor 88
        (115.5,  0.8, 88.0,  92.4),   # ATR stop = 92.4 → above floor
        (150.0,  0.8, 88.0, 120.0),   # ATR stop = 120
        (300.0,  0.8, 88.0, 200.0),   # ATR stop = 240 → capped at 250 → 200
    ],
)
def test_runtime_stop_uses_configured_floor(
    atr: float, mult: float, stop_fixed: float, expected_stop: float,
):
    strategy = _make_strategy(atr_mult_stop=mult, stop_fixed=stop_fixed)

    stop, _ = strategy._get_thresholds({"atr": atr})

    assert stop == pytest.approx(expected_stop, rel=0.01), \
        f"ATR={atr} mult={mult} floor={stop_fixed}: " \
        f"expected {expected_stop}, got {stop:.2f}"


@pytest.mark.parametrize(
    "atr, mult, trail_fixed, expected_trail",
    [
        (40.0,   0.5, 60.0,  60.0),   # floor applied
        (150.0,  0.5, 60.0,  75.0),   # above floor
        (200.0,  0.5, 60.0, 100.0),   # above floor
    ],
)
def test_risk_meta_trail_matches_runtime(
    atr: float, mult: float, trail_fixed: float, expected_trail: float,
):
    """final_trail_dist in _get_risk_meta must equal _get_thresholds result."""
    strategy = _make_strategy(atr_mult_trail=mult, trail_fixed=trail_fixed)
    bar = {"atr": atr}

    _, runtime_trail = strategy._get_thresholds(bar)
    meta = strategy._get_risk_meta(bar)

    assert meta["final_trail_dist"] == pytest.approx(runtime_trail, rel=0.01), \
        f"Risk meta trail {meta['final_trail_dist']} != runtime {runtime_trail}"
    assert meta["final_trail_dist"] == pytest.approx(expected_trail, rel=0.01)


def test_no_atr_falls_back_to_fixed():
    """When ATR is None/0/NaN and NO last-stable ATR, both runtime and risk_meta
    use the configured fixed fallback. (ATR NaN with a cached _last_atr carries
    the last stable value over — that is by design, 2026-06-26.)
    """
    strategy = _make_strategy(trail_fixed=60.0, stop_fixed=88.0)
    strategy._last_atr = 0.0  # no cached ATR -> pure fixed fallback

    for bad_atr in [None, 0, float("nan")]:
        import math
        atr_val = bad_atr if not (isinstance(bad_atr, float) and math.isnan(bad_atr)) else bad_atr
        bar = {"atr": atr_val} if not (isinstance(atr_val, float) and math.isnan(atr_val)) else {"atr": bad_atr}

        stop, trail = strategy._get_thresholds(bar)
        assert stop == 88.0, f"ATR={bad_atr}: expected stop=88, got {stop}"
        assert trail == 60.0, f"ATR={bad_atr}: expected trail=60, got {trail}"
