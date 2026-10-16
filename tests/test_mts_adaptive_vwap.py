"""2026-10-16 Hermes Agent: numerical, temporal and configuration contracts."""
from dataclasses import replace
from datetime import datetime, timezone
import math
import time

import pytest

from strategies.plugins.futures.active.mts_adaptive_vwap import (
    AdaptiveVWAP, AdaptiveVWAPConfig, AdaptiveVWAPSnapshot, parse_ts,
)


def compute(**overrides):
    args = dict(ts=300, oi_intensity=1.5, oi_direction=0.65, vol_quality=75,
                vol_zscore=0.8, vwap=110, bars=[{"ts": 300, "close": 100}])
    args.update(overrides)
    return AdaptiveVWAP.compute(**args)


def test_chips_strength_calculation():
    assert AdaptiveVWAP._chips_strength(1.5, 0.65, 75, 0.8) == pytest.approx(46.4166666667)


@pytest.mark.parametrize("index,expected", [(0, 15), (1, 30), (2, 75), (3, 63.3333333333)])
def test_each_signal_has_independent_weight(index, expected):
    cfg = AdaptiveVWAPConfig(weights=tuple(float(i == index) for i in range(4)))
    assert AdaptiveVWAP._chips_strength(1.5, 0.65, 75, 0.8, cfg) == pytest.approx(expected)


@pytest.mark.parametrize("direction,expected", [(0, 100), (0.5, 0), (1, 100), (0.25, 50)])
def test_direction_strength_follows_spec_prose(direction, expected):
    cfg = AdaptiveVWAPConfig(weights=(0, 1, 0, 0))
    assert AdaptiveVWAP._chips_strength(0, direction, 0, 0, cfg) == expected


@pytest.mark.parametrize("z,expected", [(-2, 0), (-1, 100 / 3), (0, 50), (2, 250 / 3), (2.5, 90), (3, 100)])
def test_anomaly_reference_boundaries(z, expected):
    cfg = AdaptiveVWAPConfig(weights=(0, 0, 0, 1))
    assert AdaptiveVWAP._chips_strength(0, 0.5, 0, z, cfg) == pytest.approx(expected)


def test_alpha_bounds_midpoint_and_stable_extremes():
    values = [AdaptiveVWAP._compute_alpha(x) for x in range(101)]
    assert all(0 <= x <= 1 for x in values)
    assert values == sorted(values)
    assert values[50] == 0.5
    assert values[0] == pytest.approx(0.0344451957)
    assert values[-1] == pytest.approx(0.9655548043)
    tiny = AdaptiveVWAPConfig(alpha_scale=1e-300)
    assert AdaptiveVWAP._compute_alpha(0, tiny) == 0
    assert AdaptiveVWAP._compute_alpha(100, tiny) == 1
    with pytest.raises(ValueError):
        AdaptiveVWAP._compute_alpha(float("nan"))


def test_completed_bar_at_decision_has_full_time_weight():
    bars = [{"ts": 0, "close": 9999}, {"ts": 300, "close": 123}, {"ts": 301, "close": 9999}]
    assert AdaptiveVWAP._compute_twap(bars, 300) == 123


def test_irregular_durations_and_ordering():
    bars = [{"ts": 300, "start_ts": 100, "close": 20},
            {"ts": 100, "duration_secs": 100, "close": 10}]
    original = list(bars)
    assert AdaptiveVWAP._compute_twap(bars, 300) == pytest.approx(50 / 3)
    assert bars == original


def test_gaps_and_duplicate_overlap_do_not_create_time():
    bars = [{"ts": 100, "duration_secs": 100, "close": 999},
            {"ts": 100, "duration_secs": 100, "close": 10},
            {"ts": 300, "duration_secs": 100, "close": 20}]
    assert AdaptiveVWAP._compute_twap(bars, 300) == 15
    # With overlapping defaults, intervals are clipped rather than counted twice.
    assert AdaptiveVWAP._compute_twap([{"ts": 100, "close": 10}, {"ts": 300, "close": 20}], 300) == pytest.approx(50 / 3)


def test_twap_vs_vwap_ignores_volume_spikes():
    bars = [{"ts": 100, "duration_secs": 100, "close": 10, "volume": 1},
            {"ts": 300, "duration_secs": 200, "close": 20, "volume": 1}]
    before = AdaptiveVWAP._compute_twap(bars, 300)
    bars[1]["volume"] = 1e12
    assert AdaptiveVWAP._compute_twap(bars, 300) == before


def test_invalid_or_empty_bars_are_unavailable():
    invalid = [None, {"ts": "bad", "close": 1}, {"ts": 300, "close": -1},
               {"ts": 300, "close": "bad"}, {"ts": 300, "close": 5, "start_ts": "bad"},
               {"ts": 300, "close": 5, "duration_secs": 0}]
    assert AdaptiveVWAP._compute_twap(invalid, 300) is None
    assert AdaptiveVWAP._compute_twap(None, 300) is None
    for window in (0, -1, float("inf")):
        with pytest.raises(ValueError):
            AdaptiveVWAP._compute_twap([], 300, window)


@pytest.mark.parametrize("key,value", [("ts", "bad"), ("vwap", 0), ("vwap", float("inf")),
    ("oi_intensity", -1), ("oi_direction", 2), ("vol_quality", 101), ("vol_zscore", float("nan")),
    ("vol_quality", True), ("oi_intensity", None), ("bars", []), ("window_secs", 0)])
def test_bad_inputs_fail_closed(key, value):
    assert compute(**{key: value}) is None


@pytest.mark.parametrize("alpha", [0, 0.2, 0.5, 0.8, 1])
def test_adaptive_price_interpolation(alpha):
    assert AdaptiveVWAP.compute_adaptive_price(110, 100, alpha) == 100 + 10 * alpha


@pytest.mark.parametrize("args", [(None, 100, .5), (100, None, .5), (100, 100, 2), (100, 0, .5)])
def test_invalid_price_interpolation(args):
    assert AdaptiveVWAP.compute_adaptive_price(*args) is None


def test_snapshot_confidence_and_serialization():
    result = compute(bars=[{"ts": 300, "close": 100, "duration_secs": 60}])
    assert isinstance(result, AdaptiveVWAPSnapshot)
    assert result.decision_confidence == 15  # 75 quality * 20% temporal coverage
    assert result.twap <= result.adaptive_price <= result.vwap
    assert result.to_dict()["alpha"] == round(result.alpha, 3)
    assert compute(ts="1970-01-01T00:05:00+00:00").ts == 300


@pytest.mark.parametrize("value,expected", [(300, 300), ("300.0", 300), ("1792029600000", 1792029600), (1792029600000, 1792029600),
    ("2026-10-15T10:00:00", 1792029600), ("2026-10-15T02:00:00+00:00", 1792029600),
    (datetime(2026, 10, 15, 2, tzinfo=timezone.utc), 1792029600),
    (datetime(2026, 10, 15, 10), 1792029600), (True, None), (None, None), ("bad", None),
    (object(), None), (float("nan"), None)])
def test_timestamp_normalization(value, expected):
    assert parse_ts(value) == expected


@pytest.mark.parametrize("kwargs", [{"window_secs": 0}, {"alpha_scale": None}, {"alpha_midpoint": 101},
    {"weights": [1]}, {"weights": [1, 1, 1, 1]}, {"weights": [None, 0, 0, 1]},
    {"weights": [-1, 1, 0, 1]}, {"weights": None}, {"shadow_enabled": "true"}])
def test_configuration_validation(kwargs):
    with pytest.raises(ValueError):
        AdaptiveVWAPConfig(**kwargs)


def test_configuration_file_and_unknown_fields(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("alpha_scale: 10\nshadow_enabled: false\nweights: [1, 0, 0, 0]\n")
    cfg = AdaptiveVWAPConfig.load(path)
    assert cfg.alpha_scale == 10
    assert cfg.shadow_enabled is False
    assert compute(config=cfg).chips_strength == 15
    assert AdaptiveVWAPConfig.from_mapping({"bar_secs": "60"}).bar_secs == 60
    with pytest.raises(ValueError):
        AdaptiveVWAPConfig.from_mapping([])
    with pytest.raises(ValueError):
        AdaptiveVWAPConfig.from_mapping({"unexpected": 1})
    assert replace(cfg, alpha_midpoint=15).alpha_midpoint == 15


def test_per_decision_performance_under_ten_ms():
    bars = [{"ts": 300 - i * 300, "close": 100 + i, "volume": 1} for i in range(320)]
    compute(bars=bars)
    timings = []
    for _ in range(100):
        start = time.perf_counter()
        assert compute(bars=bars) is not None
        timings.append(time.perf_counter() - start)
    assert max(timings) < .010, f"slowest adaptive compute: {max(timings) * 1000:.3f}ms"
