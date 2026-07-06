"""Unit tests for squeeze pattern classification."""
import pytest
import pandas as pd
import numpy as np
from strategies.stocks.squeeze_patterns import (
    classify_houyi,
    classify_whale,
    apply_squeeze_patterns,
)


def make_test_df(n=50, **overrides):
    """Create a test DataFrame with default indicator columns."""
    df = pd.DataFrame({
        "Close": np.linspace(100, 110, n),
        "Volume": np.random.uniform(1000, 5000, n),
        "sqz_on": [False] * 30 + [True] * 10 + [False] * 10,
        "fired": [False] * 39 + [True] + [False] * 10,
        "mom_state": [0] * 35 + [1, 2, 3, 4, 3] + [0] * 10,
        "mom_velo": np.random.uniform(-1, 1, n),
        "bullish_align": [False] * n,
        "bearish_align": [False] * n,
        "adx": np.full(n, 20.0),
    }, index=pd.date_range("2026-01-01", periods=n, freq="5min"))

    for k, v in overrides.items():
        df[k] = v

    return df


class TestClassifyHouyi:
    """Houyi pattern detection tests."""

    def test_houyi_detected_on_fired_with_momentum(self):
        df = make_test_df()
        df.loc[df.index[39], "fired"] = True
        df.loc[df.index[39], "mom_state"] = 3
        df.loc[df.index[39], "mom_velo"] = 2.0

        result = classify_houyi(df)
        assert bool(result.iloc[39]) is True

    def test_houyi_not_detected_without_fired(self):
        df = make_test_df()
        df["fired"] = False

        result = classify_houyi(df)
        assert int(result.sum()) == 0

    def test_houyi_not_detected_with_negative_velocity(self):
        df = make_test_df()
        df.loc[df.index[39], "fired"] = True
        df.loc[df.index[39], "mom_state"] = 3
        df.loc[df.index[39], "mom_velo"] = -1.0

        result = classify_houyi(df)
        assert bool(result.iloc[39]) is False

    def test_houyi_not_detected_with_low_momentum(self):
        df = make_test_df()
        df.loc[df.index[39], "fired"] = True
        df.loc[df.index[39], "mom_state"] = 0
        df.loc[df.index[39], "mom_velo"] = 1.0

        result = classify_houyi(df)
        assert bool(result.iloc[39]) is False


class TestClassifyWhale:
    """Whale pattern detection tests."""

    def test_whale_detected_on_bullish_with_volume(self):
        df = make_test_df()
        df["bullish_align"] = True
        # Create a volume spike pattern: most bars low, some bars very high
        df["Volume"] = 100.0
        df.loc[df.index[30:], "Volume"] = 10000.0  # Spike in second half

        result = classify_whale(df)
        assert int(result.sum()) > 0

    def test_whale_detected_on_bearish_with_volume(self):
        df = make_test_df()
        df["bearish_align"] = True
        df["Volume"] = 100.0
        df.loc[df.index[30:], "Volume"] = 10000.0

        result = classify_whale(df)
        assert int(result.sum()) > 0

    def test_whale_not_detected_without_volume(self):
        df = make_test_df()
        df["bullish_align"] = True
        # Set volume to near-zero so it can never exceed the rolling average
        df["Volume"] = 1.0

        result = classify_whale(df)
        assert int(result.sum()) == 0

    def test_whale_respects_adx_filter(self):
        df = make_test_df()
        df["bullish_align"] = True
        df["Volume"] = df["Volume"].rolling(20).mean() * 2.0
        df["adx"] = 5.0  # Weak trend

        result = classify_whale(df)
        assert result.sum() == 0


class TestApplySqueezePatterns:
    """Pattern application tests."""

    def test_output_has_pattern_column(self):
        df = make_test_df()
        result = apply_squeeze_patterns(df)
        assert "pattern" in result.columns

    def test_pattern_values_are_valid(self):
        df = make_test_df()
        result = apply_squeeze_patterns(df)
        valid = {"squeeze", "houyi", "whale", None}
        assert set(result["pattern"].unique()).issubset(valid)

    def test_houyi_priority_over_squeeze(self):
        """When both houyi and squeeze conditions met, houyi takes priority."""
        df = make_test_df()
        # Set up bar where both would trigger
        idx = df.index[39]
        df.loc[idx, "sqz_on"] = True
        df.loc[idx, "fired"] = True
        df.loc[idx, "mom_state"] = 3
        df.loc[idx, "mom_velo"] = 2.0

        result = apply_squeeze_patterns(df)
        assert result.loc[idx, "pattern"] == "houyi"

    def test_dataframe_not_mutated(self):
        """Original DataFrame should not be modified (copy semantics)."""
        df = make_test_df()
        original_cols = set(df.columns)
        _ = apply_squeeze_patterns(df)
        assert "pattern" not in df.columns  # Original unchanged
