"""
V-Model Verification: strategies.plugins.futures.experimental.orb_ml.py (V3 Clean)
Verifies AI V3 logic (No-Kalman) and feature vector alignment.
"""
import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch, mock_open
from core.strategy_context import StrategyContext, MarketData, PositionView
from strategies.plugins.futures.experimental.orb_ml import ORBML

def test_orb_ml_v3_logic():
    mock_model = MagicMock()
    strat = ORBML()
    
    # 1. Mock V3 Model Loading
    with patch("builtins.open", mock_open(read_data=b"dummy")), \
         patch("strategies.plugins.futures.experimental.orb_ml.pickle.load", return_value=mock_model), \
         patch("strategies.plugins.futures.experimental.orb_ml.Path.exists", return_value=True):
        
        strat.init(StrategyContext(market=None, position=None, config={"params": {}}))
    
    # Ensure it's using the V3 logic (No kalman in metadata)
    assert "kalman" not in strat.metadata["indicators"]
    
    # 2. Setup Breakout State
    strat._range_built = True
    strat._range_high = 20000
    strat._range_low = 19900
    strat._last_session = "2026-04-12"
    strat._gap_p = 0.005 # 0.5% Gap
    
    # 3. Create V3 Data (No kalman_close needed)
    df = pd.DataFrame({
        "Close": [20010],
        "High": [20010],
        "Low": [20010],
        "atr": [100],
        "lr_curve": [0.05],
        "trading_day": ["2026-04-12"]
    }, index=pd.to_datetime(["2026-04-12 09:15:00"]))
    
    ctx = StrategyContext(
        market=MarketData(last_bar=df.iloc[-1].to_dict(), df_5m=df, regime="TRENDING"),
        position=PositionView(size=0),
        config={"params": {"prob_threshold": 0.6}}
    )

    # 4. Mock AI Prediction (Success Probability = 0.9)
    # The feature vector must have 5 elements: dir, lr_curve, atr_n, gap_p, hour
    mock_model.predict_proba.return_value = np.array([[0.1, 0.9]])
    
    sig = strat.on_bar(ctx)
    
    # 5. Assertions
    assert sig is not None
    assert sig.quantity == 3 # 90% confidence -> 3 lots
    assert sig.action == "BUY"
    
    # Verify the feature vector passed to the model
    # We check the first call to predict_proba
    called_features = mock_model.predict_proba.call_args[0][0]
    assert "k_vel" not in called_features.columns
    assert "gap_p" in called_features.columns
    assert called_features["lr_curve"].iloc[0] == 0.05
