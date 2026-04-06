"""Integration tests for signal_generator with squeeze strategy filters."""
import pytest
import pandas as pd
import numpy as np
from core.strategy_schema import StrategyParams, TW_STRATEGY_PRESETS
from backtest.signal_generator import apply_strategy_filters


def make_signal_df(n=100):
    """Create a test DataFrame suitable for signal generation."""
    df = pd.DataFrame({
        "Close": np.linspace(100, 120, n),
        "Volume": np.random.uniform(1000, 5000, n),
        "sqz_on": [False] * 50 + [True] * 20 + [False] * 30,
        "fired": [False] * 69 + [True] + [False] * 30,
        "mom_state": [0] * 60 + [1, 2, 3, 4, 3, 2, 1] + [0] * 33,
        "mom_velo": np.random.uniform(-1, 1, n),
        "bullish_align": [False] * n,
        "bearish_align": [False] * n,
        "adx": np.full(n, 20.0),
        "value_score": np.random.uniform(0.3, 0.9, n),
        "market_regime": ["bull_trend"] * n,
    }, index=pd.date_range("2026-01-01", periods=n, freq="5min"))

    # Add pattern column
    from strategies.stocks.squeeze_patterns import apply_squeeze_patterns
    df = apply_squeeze_patterns(df)

    return df


class TestSignalGeneratorIntegration:
    """Integration tests for squeeze filters in signal_generator."""

    def test_apply_filters_squeeze_only(self):
        df = make_signal_df()
        params = TW_STRATEGY_PRESETS["squeeze_only"]

        result = apply_strategy_filters(df, params)
        assert len(result) < len(df)  # Should filter some rows
        # All remaining rows should have sqz_on=True
        if len(result) > 0:
            assert all(result["sqz_on"])

    def test_apply_filters_conservative(self):
        df = make_signal_df()
        params = TW_STRATEGY_PRESETS["conservative"]

        result = apply_strategy_filters(df, params)
        assert len(result) < len(df)

    def test_apply_filters_baseline_no_reduction(self):
        """Baseline accepts all three patterns, filters out None-pattern rows."""
        df = make_signal_df()
        params = TW_STRATEGY_PRESETS["baseline"]

        result = apply_strategy_filters(df, params)
        # Baseline only filters by patterns (squeeze/houyi/whale), not by momentum/regime
        # Should only keep rows where pattern was classified
        assert len(result) > 0
        assert all(result["pattern"].isin(["squeeze", "houyi", "whale"]))

    def test_apply_filters_custom_no_change(self):
        """Custom (empty) params should not filter anything."""
        df = make_signal_df()
        params = TW_STRATEGY_PRESETS["custom"]

        result = apply_strategy_filters(df, params)
        assert len(result) == len(df)
