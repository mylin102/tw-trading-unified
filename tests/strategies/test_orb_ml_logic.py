"""
V-Model Verification: strategies/plugins/futures/orb_ml.py
Verifies AI-based position sizing and inference logic with stable session data.
"""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch, mock_open
from core.strategy_context import StrategyContext, MarketData, PositionView
from strategies.plugins.futures.orb_ml import ORBML

def test_orb_ml_position_sizing():
    mock_model = MagicMock()
    strat = ORBML()
    
    with patch("builtins.open", mock_open(read_data=b"dummy")), \
         patch("strategies.plugins.futures.orb_ml.pickle.load", return_value=mock_model), \
         patch("strategies.plugins.futures.orb_ml.Path.exists", return_value=True):
        
        strat.init(StrategyContext(market=None, position=None, config={"params": {}}))
    
    # 1. Manually set range state to skip range-building phase
    strat._range_built = True
    strat._range_high = 30000
    strat._range_low = 29900
    strat._last_session = "2026-01-01" # Set to avoid auto-reset
    
    # 2. Create dummy market data with session info
    df = pd.DataFrame({
        "Close": [30001, 30005, 30010],
        "High": [30001, 30005, 30010],
        "Low": [30001, 30005, 30010],
        "kalman_close": [30000, 30002, 30005],
        "atr": [50, 50, 50],
        "lr_curve": [0.1, 0.1, 0.1],
        "trading_day": ["2026-01-01"] * 3 # Match _last_session
    }, index=pd.date_range("2026-01-01", periods=3, freq="5min"))
    
    ctx = StrategyContext(
        market=MarketData(last_bar=df.iloc[-1].to_dict(), df_5m=df),
        position=PositionView(size=0),
        config={"params": {}}
    )

    # TEST A: High Confidence (85%+) -> 3 lots
    mock_model.predict_proba.return_value = np.array([[0.1, 0.9]])
    sig = strat.on_bar(ctx)
    assert sig is not None, "Signal should trigger on breakout"
    assert sig.quantity == 3
    
    # TEST B: Medium-High Confidence (75%+) -> 2 lots
    strat._signaled = False
    mock_model.predict_proba.return_value = np.array([[0.2, 0.8]])
    sig = strat.on_bar(ctx)
    assert sig.quantity == 2
    
    # TEST C: Medium Confidence (65%+) -> 1 lot
    strat._signaled = False
    mock_model.predict_proba.return_value = np.array([[0.3, 0.7]])
    sig = strat.on_bar(ctx)
    assert sig.quantity == 1
