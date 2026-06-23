"""Regression tests for adaptive_orb trend-quality filters."""
from unittest.mock import MagicMock, mock_open, patch

import numpy as np
import pandas as pd

from core.market_regime import MarketRegime
from core.strategy_context import MarketData, PositionView, StrategyContext
from strategies.plugins.futures.deprecated.adaptive_orb import AdaptiveORB


def _make_strategy(mock_model=None):
    strategy = AdaptiveORB()
    model = mock_model or MagicMock()
    with patch("builtins.open", mock_open(read_data=b"dummy")), \
         patch("strategies.plugins.futures.deprecated.adaptive_orb.pickle.load", return_value=model), \
         patch("strategies.plugins.futures.deprecated.adaptive_orb.Path.exists", return_value=True):
        strategy.init(StrategyContext(market=None, position=None, config={"params": {"prob_threshold": 0.6}}))
    return strategy, model


def _make_context(volumes):
    df = pd.DataFrame({
        "Open": [19970, 19978, 19988, 19996, 20002, 20006],
        "Close": [19980, 19988, 19996, 20002, 20006, 20010],
        "High": [19982, 19990, 19998, 20004, 20008, 20012],
        "Low": [19968, 19976, 19986, 19994, 20000, 20004],
        "Volume": volumes,
        "vwap": [19970, 19978, 19986, 19994, 20000, 20005],
        "lr_curve": [0.02, 0.025, 0.03, 0.035, 0.04, 0.05],
        "atr": [100, 100, 100, 100, 100, 100],
        "trading_day": ["2026-04-12"] * 6,
    }, index=pd.to_datetime([
        "2026-04-12 09:00:00",
        "2026-04-12 09:05:00",
        "2026-04-12 09:10:00",
        "2026-04-12 09:15:00",
        "2026-04-12 09:20:00",
        "2026-04-12 09:25:00",
    ]))
    return StrategyContext(
        market=MarketData(last_bar=df.iloc[-1].to_dict(), df_5m=df, regime=MarketRegime.TRENDING),
        position=PositionView(size=0),
        config={"params": {"prob_threshold": 0.6}},
    )


def test_adaptive_orb_trending_breakout_requires_quality_confirmation():
    strategy, model = _make_strategy()
    strategy._range_built = True
    strategy._range_high = 20000
    strategy._range_low = 19900
    strategy._last_session = "2026-04-12"
    strategy._gap_p = 0.005
    model.predict_proba.return_value = np.array([[0.1, 0.9]])

    weak_ctx = _make_context([100, 100, 100, 100, 100, 105])

    assert strategy.on_bar(weak_ctx) is None


def test_adaptive_orb_trending_breakout_accepts_supported_trend():
    strategy, model = _make_strategy()
    strategy._range_built = True
    strategy._range_high = 20000
    strategy._range_low = 19900
    strategy._last_session = "2026-04-12"
    strategy._gap_p = 0.005
    model.predict_proba.return_value = np.array([[0.1, 0.9]])

    strong_ctx = _make_context([100, 100, 100, 100, 100, 180])
    signal = strategy.on_bar(strong_ctx)

    assert signal is not None
    assert signal.action == "BUY"
    assert signal.reason == "ADAPTIVE_TREND_V3"
