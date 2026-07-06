"""Tests for KbarFeature strategy plugin.

Covers:
- Flat position → no signal when conditions not met
- Bear regime + bearish alignment + momentum → SELL signal
- Bull regime + bullish alignment + momentum → BUY signal
- Exit: stop loss hit, take profit hit, VWAP+MACD reversal, max hold
- Missing required columns → HOLD signal
- Invalid ATR → None
"""
from __future__ import annotations

import pytest

from core.strategy_context import MarketData, PositionView, StrategyContext
from core.signal import Signal
from strategies.plugins.futures.active.kbar_feature import KbarFeature


def _make_bar(**overrides) -> dict:
    """Create a feature-enriched bar dict with defaults that DON'T trigger signals.

    NOTE: overrides are case-insensitive for Close/High/Low — writing
    ``close=17900.0`` is automatically mapped to ``\"Close\": 17900.0``.
    """
    # Normalise case-sensitive keys so test authors never trip on this.
    _KEY_MAP = {"close": "Close", "high": "High", "low": "Low"}
    normalised = {}
    for k, v in overrides.items():
        normalised[_KEY_MAP.get(k, k)] = v

    defaults = {
        "Close": 18000.0,
        "High": 18020.0,
        "Low": 17980.0,
        "atr": 200.0,
        "vwap": 18000.0,
        "adx": 15.0,
        "score": 0.0,
        "regime": "NEUTRAL",
        "bear_align": False,
        "bull_align": False,
        "bearish_align": False,
        "bullish_align": False,
        "macd_hist": 0.0,
        "macd_rising": False,
        "mom_velo": 0.0,
        "recent_high": 18200.0,
        "recent_low": 17800.0,
        "price_vs_vwap": 0.0,
        "volume_spike": False,
    }
    defaults.update(normalised)
    return defaults


def _make_ctx(bar: dict | None = None, pos_size: int = 0, entry_price: float = 0.0,
              stop_loss: float | None = None,
              config: dict | None = None) -> StrategyContext:
    """Create a StrategyContext for testing.

    Parameters
    ----------
    bar : dict, optional
        Bar data (defaults to neutral bar).
    pos_size : int
        Position size (0 = flat).
    entry_price : float
        Entry price.
    stop_loss : float | None, optional
        Current stop loss price. Required for exit tests that check stop hits.
    config : dict, optional
        Strategy config overrides.
    """
    if bar is None:
        bar = _make_bar()
    return StrategyContext(
        market=MarketData(last_bar=bar),
        position=PositionView(size=pos_size, entry_price=entry_price,
                              current_stop_loss=stop_loss),
        config=config or {"params": {}},
    )


class TestKbarFeatureEntry:
    """Entry signal tests."""

    def setup_method(self):
        self.strategy = KbarFeature()
        self.strategy.init(_make_ctx())

    def test_flat_no_signal_when_neutral(self):
        """Flat position + neutral regime → no signal."""
        ctx = _make_ctx(_make_bar(regime="NEUTRAL"))
        assert self.strategy.on_bar(ctx) is None

    def test_short_entry_full_conditions(self):
        """Bear regime + bearish alignment + momentum → SELL signal."""
        bar = _make_bar(
            regime="BEAR",
            bear_align=True,
            bearish_align=True,
            adx=25.0,
            score=-30.0,
            macd_hist=-10.0,
            mom_velo=-5.0,
            close=17900.0,
            vwap=18000.0,
            recent_low=17950.0,
        )
        ctx = _make_ctx(bar)
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "SELL"
        assert sig.reason == "KBAR_FEATURE_SHORT"
        assert sig.stop_loss > bar["Close"]  # SL above close for short
        assert sig.target < bar["Close"]     # TP below close for short
        ok, msg = sig.validate()
        assert ok, msg

    def test_short_blocked_by_weak_adx(self):
        """Bear regime but ADX below threshold → no signal."""
        bar = _make_bar(
            regime="BEAR",
            bear_align=True,
            bearish_align=True,
            adx=15.0,  # below default 20
            score=-30.0,
            macd_hist=-10.0,
            mom_velo=-5.0,
            close=17900.0,
            vwap=18000.0,
        )
        ctx = _make_ctx(bar)
        assert self.strategy.on_bar(ctx) is None

    def test_short_blocked_by_no_alignment(self):
        """Bear regime but no alignment → no signal."""
        bar = _make_bar(
            regime="BEAR",
            bear_align=False,
            bearish_align=False,
            adx=25.0,
            score=-30.0,
            macd_hist=-10.0,
            mom_velo=-5.0,
            close=17900.0,
            vwap=18000.0,
        )
        ctx = _make_ctx(bar)
        assert self.strategy.on_bar(ctx) is None

    def test_short_blocked_by_price_above_vwap(self):
        """Bear regime but price above VWAP → no signal."""
        bar = _make_bar(
            regime="BEAR",
            bear_align=True,
            bearish_align=True,
            adx=25.0,
            score=-30.0,
            macd_hist=-10.0,
            mom_velo=-5.0,
            close=18100.0,  # above vwap=18000
            vwap=18000.0,
        )
        ctx = _make_ctx(bar)
        assert self.strategy.on_bar(ctx) is None

    def test_short_blocked_by_score_above_threshold(self):
        """Bear regime but score too high → no signal."""
        bar = _make_bar(
            regime="BEAR",
            bear_align=True,
            bearish_align=True,
            adx=25.0,
            score=-10.0,  # above threshold -20
            macd_hist=-10.0,
            mom_velo=-5.0,
            close=17900.0,
            vwap=18000.0,
        )
        ctx = _make_ctx(bar)
        assert self.strategy.on_bar(ctx) is None

    def test_short_blocked_by_momentum(self):
        """Bear regime but MACD positive → no signal."""
        bar = _make_bar(
            regime="BEAR",
            bear_align=True,
            bearish_align=True,
            adx=25.0,
            score=-30.0,
            macd_hist=5.0,   # positive, not bearish momentum
            mom_velo=3.0,
            close=17900.0,
            vwap=18000.0,
        )
        ctx = _make_ctx(bar)
        assert self.strategy.on_bar(ctx) is None

    def test_short_require_breakout_not_met(self):
        """require_breakout=True but close >= recent_low → no signal."""
        bar = _make_bar(
            regime="BEAR",
            bear_align=True,
            bearish_align=True,
            adx=25.0,
            score=-30.0,
            macd_hist=-10.0,
            mom_velo=-5.0,
            close=17900.0,
            vwap=18000.0,
            recent_low=17850.0,  # close > recent_low, no breakdown
        )
        ctx = _make_ctx(bar)
        assert self.strategy.on_bar(ctx) is None

    def test_long_entry_full_conditions(self):
        """Bull regime + bullish alignment + momentum → BUY signal."""
        bar = _make_bar(
            regime="BULL",
            bull_align=True,
            bullish_align=True,
            adx=25.0,
            score=30.0,
            macd_hist=10.0,
            macd_rising=True,
            mom_velo=5.0,
            close=18100.0,
            vwap=18000.0,
            recent_high=18050.0,
        )
        ctx = _make_ctx(bar, config={"params": {"long_enabled": True}})
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "BUY"
        assert sig.reason == "KBAR_FEATURE_LONG"
        assert sig.stop_loss < bar["Close"]  # SL below close for long
        assert sig.target > bar["Close"]     # TP above close for long
        ok, msg = sig.validate()
        assert ok, msg

    def test_long_disabled_by_default(self):
        """long_enabled=False → no LONG signal even in bull regime."""
        bar = _make_bar(
            regime="BULL",
            bull_align=True,
            bullish_align=True,
            adx=25.0,
            score=30.0,
            macd_hist=10.0,
            macd_rising=True,
            mom_velo=5.0,
            close=18100.0,
            vwap=18000.0,
            recent_high=18050.0,
        )
        ctx = _make_ctx(bar)  # no config override, long_enabled defaults to False
        assert self.strategy.on_bar(ctx) is None

    def test_position_already_open_no_entry(self):
        """Has position → no entry signal, goes to exit evaluation."""
        bar = _make_bar(
            regime="BEAR",
            bear_align=True,
            bearish_align=True,
            adx=25.0,
            score=-30.0,
            macd_hist=-10.0,
            mom_velo=-5.0,
            close=17900.0,
            vwap=18000.0,
            recent_low=17950.0,
        )
        ctx = _make_ctx(bar, pos_size=-1, entry_price=17900.0)
        sig = self.strategy.on_bar(ctx)
        # Should return exit evaluation, not a new entry
        assert sig is None or sig.action in ("EXIT", "HOLD")

    def test_size_mult_increases_with_adx(self):
        """Test the _size_mult helper."""
        assert self.strategy._size_mult(15.0, 0.0, False) == 1.0
        assert self.strategy._size_mult(25.0, 0.0, False) == 1.25
        assert self.strategy._size_mult(30.0, 0.0, False) == 1.50
        assert self.strategy._size_mult(15.0, 80.0, False) == 1.25
        assert self.strategy._size_mult(15.0, 0.0, True) == 1.10
        assert self.strategy._size_mult(30.0, 80.0, True) == 1.75  # cap


class TestKbarFeatureExit:
    """Exit signal tests."""

    def setup_method(self):
        self.strategy = KbarFeature()
        self.strategy.init(_make_ctx())

    def _enter_short(self):
        """Simulate having entered a short position by setting bars_held."""
        self.strategy._bars_held = 1

    def test_exit_short_stop_hit(self):
        """Short position + stop loss hit → EXIT."""
        self._enter_short()
        bar = _make_bar(High=18250.0, Low=17900.0, Close=18200.0)
        ctx = _make_ctx(bar, pos_size=-1, entry_price=18000.0, stop_loss=18240.0)
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "EXIT"
        assert "STOP" in sig.reason

    def test_exit_short_tp_hit(self):
        """Short position + take profit hit → EXIT."""
        self._enter_short()
        bar = _make_bar(High=18000.0, Low=17500.0, Close=17550.0, atr=200.0)
        ctx = _make_ctx(bar, pos_size=-1, entry_price=18000.0)
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "EXIT"
        assert "TP" in sig.reason

    def test_exit_short_vwap_reversal(self):
        """Short position + close > VWAP and MACD > 0 → EXIT."""
        self._enter_short()
        bar = _make_bar(
            close=18100.0, High=18120.0, Low=17980.0,
            vwap=18000.0, macd_hist=5.0, mom_velo=3.0,
        )
        ctx = _make_ctx(bar, pos_size=-1, entry_price=18000.0)
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "EXIT"
        assert "RECLAIMED_VWAP" in sig.reason

    def test_exit_short_momentum_reversal(self):
        """Short position + mom_velo > 0 → EXIT."""
        self._enter_short()
        bar = _make_bar(
            Close=18000.0, High=18010.0, Low=17990.0,
            vwap=18000.0, macd_hist=-2.0, mom_velo=3.0,
        )
        ctx = _make_ctx(bar, pos_size=-1, entry_price=18000.0)
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "EXIT"
        assert "MOM_REV" in sig.reason

    def test_exit_short_max_hold(self):
        """Short position + max hold bars reached → EXIT."""
        bar = _make_bar(Close=18000.0)
        ctx = _make_ctx(bar, pos_size=-1, entry_price=18000.0)
        # Force bars_held past max_hold_bars (default 12)
        self.strategy._bars_held = 15
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "EXIT"
        assert "MAX_HOLD" in sig.reason


class TestKbarFeatureEdgeCases:
    """Edge case tests."""

    def setup_method(self):
        self.strategy = KbarFeature()
        self.strategy.init(_make_ctx())

    def test_missing_columns_returns_hold(self):
        """Bar missing required columns → HOLD signal (not crash)."""
        bar = {"Close": 18000.0, "High": 18020.0, "Low": 17980.0}  # missing most columns
        ctx = _make_ctx(bar)
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "HOLD"
        assert "MISSING_COLUMNS" in sig.reason

    def test_atr_zero_returns_none(self):
        """ATR == 0 → None (no signal, no crash)."""
        bar = _make_bar(atr=0.0)
        ctx = _make_ctx(bar)
        assert self.strategy.on_bar(ctx) is None

    def test_atr_nan_returns_none(self):
        """ATR == NaN → None."""
        import math
        bar = _make_bar(atr=math.nan)
        ctx = _make_ctx(bar)
        assert self.strategy.on_bar(ctx) is None

    def test_cleanup_resets_state(self):
        """cleanup() resets bars_held."""
        self.strategy._bars_held = 10
        self.strategy.cleanup()
        assert self.strategy._bars_held == 0

    def test_config_params_override_defaults(self):
        """Config params override strategy defaults."""
        config = {"params": {"adx_threshold": 30.0}}
        bar = _make_bar(
            regime="BEAR", bear_align=True, bearish_align=True,
            adx=25.0,  # below 30 threshold
            score=-30.0, macd_hist=-10.0, mom_velo=-5.0,
            close=17900.0, vwap=18000.0, recent_low=17950.0,
        )
        ctx = _make_ctx(bar, config=config)
        assert self.strategy.on_bar(ctx) is None  # blocked by adx_threshold=30

    def test_regime_weak_triggers_short(self):
        """regime='WEAK' should trigger short just like 'BEAR'."""
        bar = _make_bar(
            regime="WEAK",
            bear_align=True, bearish_align=True,
            adx=25.0, score=-30.0,
            macd_hist=-10.0, mom_velo=-5.0,
            close=17900.0, vwap=18000.0, recent_low=17950.0,
        )
        ctx = _make_ctx(bar)
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "SELL"

    def test_regime_down_triggers_short(self):
        """regime='DOWN' should trigger short just like 'BEAR'."""
        bar = _make_bar(
            regime="DOWN",
            bear_align=True, bearish_align=True,
            adx=25.0, score=-30.0,
            macd_hist=-10.0, mom_velo=-5.0,
            close=17900.0, vwap=18000.0, recent_low=17950.0,
        )
        ctx = _make_ctx(bar)
        sig = self.strategy.on_bar(ctx)
        assert sig is not None
        assert sig.action == "SELL"

    def test_name_matches_filename(self):
        """Strategy name must match filename for registry discovery."""
        assert self.strategy.name == "kbar_feature"

    def test_metadata_has_required_fields(self):
        """Metadata has all required fields for dashboard."""
        meta = self.strategy.metadata
        assert "asset_class" in meta
        assert "version" in meta
        assert "market_regime" in meta
        assert "description" in meta
        assert meta["asset_class"] == "futures"
