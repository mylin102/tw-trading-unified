"""
Data Enricher — Factory for computing technical indicators based on strategy requirements.
Optimized for Large Datasets (800k+ rows) using NumPy vectorization.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import numpy as np

def _calc_atr(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    length = int(kwargs.get("atr_length", 14))
    high, low, close = df["High"].values, df["Low"].values, df["Close"].values
    
    tr = np.zeros(len(close))
    tr[0] = high[0] - low[0]
    tr[1:] = np.maximum(high[1:] - low[1:], 
                        np.maximum(np.abs(high[1:] - close[:-1]), 
                                   np.abs(low[1:] - close[:-1])))
    
    atr = pd.Series(tr).rolling(window=length).mean().values
    df = df.copy()
    df["atr"] = atr
    return df

def _calc_vwap(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    """Calculate Volume Weighted Average Price with session fallback."""
    v, p = df["Volume"].values, (df["High"].values + df["Low"].values + df["Close"].values) / 3
    df = df.copy()
    if "trading_day" in df.columns:
        # Avoid complex transform for speed in large DF
        # Simple cumulative sums for the whole series if only one day, 
        # or grouped cumsum for multi-day.
        cum_pv = (p * v).groupby(df["trading_day"]).cumsum()
        cum_v = v.groupby(df["trading_day"]).cumsum()
        df["vwap"] = cum_pv / cum_v
    else:
        df["vwap"] = np.cumsum(p * v) / np.cumsum(v)
    return df

def _calc_linreg(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    """Hyper-speed Rolling Linear Regression via NumPy."""
    length = int(kwargs.get("lr_length", 20))
    y = df["Close"].values
    n = len(y)
    if n < length: return df
    
    x = np.arange(length)
    x_mean = np.mean(x)
    x_var_total = np.sum((x - x_mean)**2)
    
    y_windows = np.lib.stride_tricks.sliding_window_view(y, length)
    y_means = np.mean(y_windows, axis=1, keepdims=True)
    slopes = np.sum((x - x_mean) * (y_windows - y_means), axis=1) / x_var_total
    
    full_slopes = np.zeros(n)
    full_slopes[length-1:] = slopes
    
    df = df.copy()
    df["lr_slope"] = full_slopes
    df["lr_curve"] = pd.Series(full_slopes).diff().values
    return df

def _calc_kalman(df: pd.DataFrame, **kwargs) -> pd.DataFrame:
    from core.signal_processing import apply_kalman_filter
    q = kwargs.get("kalman_q") or kwargs.get("q") or 1e-4
    r = kwargs.get("kalman_r") or kwargs.get("r") or 0.01
    df = df.copy()
    df["kalman_close"] = apply_kalman_filter(df["Close"], q=float(q), r=float(r))
    return df

class DataEnricher:
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.registry: Dict[str, Callable] = {}
        self.logger = logger or logging.getLogger(__name__)
        self.register("atr", _calc_atr)
        self.register("vwap", _calc_vwap)
        self.register("linreg", _calc_linreg)
        self.register("kalman", _calc_kalman)
        try:
            from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
            self.register("squeeze", lambda d, **kw: calculate_futures_squeeze(d))
        except ImportError: pass

    def register(self, name: str, func: Callable): self.registry[name] = func

    def enrich(self, df: pd.DataFrame, indicators: List[str], **kwargs) -> pd.DataFrame:
        if not indicators: return df
        res = df.copy()
        for name in indicators:
            if name in self.registry:
                try: res = self.registry[name](res, **kwargs)
                except Exception as e: self.logger.error(f"Err {name}: {e}")
        return res

enricher = DataEnricher()
