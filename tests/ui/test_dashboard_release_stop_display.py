"""2026-08-14 Hermes Agent: dashboard release-stop threshold display must surface
the ATR-dynamic effective value (strategy: max(release_stop_points, ATR * atr_multiplier_stop),
tmf_spread._get_thresholds), not just the fixed config floor.
"""
import pytest


class TestReleaseStopLabel:
    def _fmt(self, stop_pts, atr, mult):
        from ui.dashboard import _fmt_release_stop_label
        return _fmt_release_stop_label(stop_pts, atr, mult)

    def test_dynamic_exceeds_floor(self):
        # atr 38.6 x 2.5 = 96.5 > floor 88 -> effective 96.5 shown with formula
        label = self._fmt(88, 38.6, 2.5)
        assert "96.5" in label
        assert "88" in label
        assert "2.5" in label
        assert "38.6" in label

    def test_floor_wins_when_atr_small(self):
        # atr 20.0 x 2.5 = 50.0 < floor 88 -> effective stays 88, formula shown
        label = self._fmt(88, 20.0, 2.5)
        assert "88.0" in label
        assert "88" in label
        assert "20.0" in label
        assert "2.5" in label

    def test_none_atr_no_dynamic_suffix(self):
        label = self._fmt(88, None, 2.5)
        assert "88" in label
        assert "動態" not in label

    def test_string_input_coerced(self):
        label = self._fmt("88", 38.6, 2.5)
        assert "96.5" in label

    def test_no_mult_no_dynamic_suffix(self):
        label = self._fmt(88, 38.6, None)
        assert "88" in label
        assert "動態" not in label


class TestTrailDistanceLabel:
    def _fmt(self, trail_pts, atr, mult):
        from ui.dashboard import _fmt_trail_distance_label
        return _fmt_trail_distance_label(trail_pts, atr, mult)

    def test_dynamic_exceeds_floor(self):
        # atr 69.7 x 1.0 = 69.7 > floor 66 -> effective 69.7 shown with formula
        label = self._fmt(66, 69.7, 1.0)
        assert "69.7" in label
        assert "66" in label
        assert "1.0" in label

    def test_floor_wins_when_atr_small(self):
        # atr 40.0 x 1.0 = 40.0 < floor 66 -> effective stays 66, formula shown
        label = self._fmt(66, 40.0, 1.0)
        assert "66.0" in label
        assert "40.0" in label
        assert "1.0" in label

    def test_none_atr_no_dynamic_suffix(self):
        label = self._fmt(66, None, 1.0)
        assert "66" in label
        assert "動態" not in label

    def test_no_mult_no_dynamic_suffix(self):
        label = self._fmt(66, 69.7, None)
        assert "66" in label
        assert "動態" not in label
