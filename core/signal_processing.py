"""
Signal Processing Utilities — Advanced denoising and filtering algorithms.
Includes Kalman Filter for zero-lag trend estimation.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


def apply_kalman_filter(series: pd.Series, q: float = 1e-4, r: float = 0.01) -> pd.Series:
    """
    Apply a 1D Kalman Filter to a price series.
    
    Args:
        series (pd.Series): Raw price data.
        q (float): Process noise (higher = more reactive, less smooth).
        r (float): Measurement noise (higher = more smooth, less reactive).
    """
    if series.empty:
        return series

    data = series.values
    n = len(data)
    
    # Initialize state with the first data point
    state_est = data[0]
    # Initial error estimate: assume first point is exactly true (0 error) 
    # or use a small value.
    error_est = r 
    
    result = np.zeros(n)
    result[0] = state_est
    
    for i in range(1, n):
        # 1. Prediction (State stays same, error increases by process noise)
        prediction = state_est
        error_prediction = error_est + q
        
        # 2. Update (Kalman Gain and Measurement integration)
        kalman_gain = error_prediction / (error_prediction + r)
        state_est = prediction + kalman_gain * (data[i] - prediction)
        error_est = (1 - kalman_gain) * error_prediction
        
        result[i] = state_est
        
    return pd.Series(result, index=series.index, name=f"{series.name}_kalman")


def apply_adaptive_kalman(df: pd.DataFrame, length: int = 20) -> pd.Series:
    """
    Experimental: Kalman Filter where measurement noise (R) is tied to ATR.
    Adapts to market volatility.
    """
    # For now, we use the standard implementation
    return apply_kalman_filter(df['Close'])
