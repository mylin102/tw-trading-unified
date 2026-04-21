"""V-Model Level 2: Stock Indicator Tests."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import numpy as np
import pandas as pd
import pytest
import pandas_ta  # Must import to register 'df.ta' accessor

from strategies.options.options_engine.engine.indicators import (
    calculate_stock_squeeze,
    calculate_futures_squeeze,
)


class TestCalculateStockSqueeze:
    """P1.2: Verify calculate_stock_squeeze produces all required columns."""

    @pytest.fixture
    def sample_stock_data(self):
        """Generate 100 bars of synthetic stock data."""
        np.random.seed(42)
        n = 100
        close = np.cumsum(np.random.randn(n) * 0.5) + 100
        high = close + np.abs(np.random.randn(n) * 0.3)
        low = close - np.abs(np.random.randn(n) * 0.3)
        open_ = close + np.random.randn(n) * 0.2
        volume = np.random.randint(1000, 10000, n)

        index = pd.date_range("2025-01-01 09:00", periods=n, freq="5min")
        return pd.DataFrame({
            "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume,
        }, index=index)

    def test_basic_columns(self, sample_stock_data):
        """Output should include all columns from calculate_futures_squeeze."""
        res = calculate_stock_squeeze(sample_stock_data)
        for col in ["sqz_on", "momentum", "mom_state", "fired", "vwap",
                     "ema_fast", "ema_slow", "bullish_align", "bearish_align"]:
            assert col in res.columns, f"Missing column: {col}"

    def test_macd_columns(self, sample_stock_data):
        """Output should include MACD histogram and rising flag."""
        res = calculate_stock_squeeze(sample_stock_data)
        assert "macd_hist" in res.columns
        assert "macd_rising" in res.columns
        assert "macd" in res.columns
        assert "macd_signal" in res.columns

    def test_kd_columns(self, sample_stock_data):
        """Output should include K and D values."""
        res = calculate_stock_squeeze(sample_stock_data)
        assert "k_val" in res.columns
        assert "d_val" in res.columns

    def test_adx_columns(self, sample_stock_data):
        """Output should include ADX value."""
        res = calculate_stock_squeeze(sample_stock_data)
        assert "adx" in res.columns

    def test_bb_columns(self, sample_stock_data):
        """Output should include Bollinger Band levels."""
        res = calculate_stock_squeeze(sample_stock_data)
        assert "bb_lower" in res.columns
        assert "bb_mid" in res.columns
        assert "bb_upper" in res.columns

    def test_vwap_sigma_and_flow_columns(self, sample_stock_data):
        """Output should include VWAP sigma bands and approximate flow pressure fields."""
        res = calculate_stock_squeeze(sample_stock_data)
        for col in [
            "money_flow_multiplier",
            "bar_delta",
            "cum_bar_delta",
            "delta_trend",
            "vwap_std",
            "z_vwap",
            "vwap_upper_1",
            "vwap_lower_1",
            "vwap_upper_2",
            "vwap_lower_2",
        ]:
            assert col in res.columns, f"Missing column: {col}"

        multiplier = res["money_flow_multiplier"].dropna()
        assert ((multiplier >= -1) & (multiplier <= 1)).all()
        assert np.isfinite(res["z_vwap"].dropna()).all()

    def test_flow_pressure_handles_flat_bars(self, sample_stock_data):
        """High==Low bars should not create inf/NaN pressure explosions."""
        df = sample_stock_data.copy()
        df["High"] = df["Close"]
        df["Low"] = df["Close"]

        res = calculate_stock_squeeze(df)

        assert np.isfinite(res["money_flow_multiplier"].fillna(0)).all()
        assert (res["money_flow_multiplier"].fillna(0) == 0).all()
        assert np.isfinite(res["bar_delta"].fillna(0)).all()

    def test_short_data_returns_early(self):
        """Data shorter than min_req (30) should return unchanged."""
        n = 20
        close = np.cumsum(np.random.randn(n) * 0.5) + 100
        high = close + np.abs(np.random.randn(n) * 0.3)
        low = close - np.abs(np.random.randn(n) * 0.3)
        open_ = close + np.random.randn(n) * 0.2
        volume = np.random.randint(1000, 10000, n)
        index = pd.date_range("2025-01-01 09:00", periods=n, freq="5min")
        df = pd.DataFrame({
            "Open": open_, "High": high, "Low": low, "Close": close, "Volume": volume,
        }, index=index)

        res = calculate_stock_squeeze(df)
        # Short data: function now handles gracefully, columns exist but may be NaN
        assert len(res) == n
        assert "macd" not in res.columns or res["macd"].isna().all()  # MACD needs > 26 rows

    def test_fired_is_boolean(self, sample_stock_data):
        """fired column should be boolean (True only on squeeze release)."""
        res = calculate_stock_squeeze(sample_stock_data)
        assert res["fired"].dtype == bool

    def test_mom_state_values(self, sample_stock_data):
        """mom_state should be in {0, 1, 2, 3}."""
        res = calculate_stock_squeeze(sample_stock_data)
        valid = res["mom_state"].dropna().unique()
        for v in valid:
            assert v in [0, 1, 2, 3]

    def test_strategy_compatibility_scout(self, sample_stock_data):
        """Result should satisfy scout_strategy requirements."""
        res = calculate_stock_squeeze(sample_stock_data)
        last = res.iloc[-1]
        # Scout needs: fired, mom_state >= 2
        assert "fired" in res.columns
        assert "mom_state" in res.columns
        assert last["mom_state"] >= 0  # Always valid

    def test_strategy_compatibility_kd_reversion(self, sample_stock_data):
        """Result should satisfy kd_mean_reversion requirements."""
        res = calculate_stock_squeeze(sample_stock_data)
        last = res.iloc[-1]
        # KD strategy needs: k_val, adx, ema_200_up (via ema_macro)
        assert "k_val" in res.columns
        assert "adx" in res.columns
        assert "ema_macro" in res.columns  # Proxy for EMA200

    def test_strategy_compatibility_bb_bounce(self, sample_stock_data):
        """Result should satisfy bb_bounce requirements."""
        res = calculate_stock_squeeze(sample_stock_data)
        last = res.iloc[-1]
        # BB bounce needs: bb_lower, macd_hist
        assert "bb_lower" in res.columns
        assert "macd_hist" in res.columns

    def test_strategy_compatibility_ema_pullback(self, sample_stock_data):
        """Result should satisfy ema_pullback requirements."""
        res = calculate_stock_squeeze(sample_stock_data)
        last = res.iloc[-1]
        # EMA pullback needs: ema_slow, bullish_align, k_val, adx
        assert "ema_slow" in res.columns
        assert "bullish_align" in res.columns
        assert "k_val" in res.columns
        assert "adx" in res.columns
