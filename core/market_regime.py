"""
Market Regime Classifier — Wave 19.
Analyzes market conditions to classify trading days into regimes.
Optimized for pre-calculation on large datasets.
"""
from __future__ import annotations
import pandas as pd
import numpy as np
from enum import Enum

class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    CHOPPY = "CHOPPY"
    SHOCK = "SHOCK"
    NEUTRAL = "NEUTRAL"

def calculate_regimes(df: pd.DataFrame) -> pd.Series:
    """
    Calculate daily regimes for the entire DataFrame.
    Returns a Series indexed by trading day.
    If required columns are missing, returns NEUTRAL for all days.
    """
    # Defensive: check required columns
    required = {"High", "Low", "Close"}
    if not required.issubset(set(df.columns)):
        # Return NEUTRAL for all days when data is insufficient
        return pd.Series("NEUTRAL", index=df.index)

    # 1. Daily Volatility (ATR)
    daily_high = df['High'].resample('D').max()
    daily_low = df['Low'].resample('D').min()
    daily_close = df['Close'].resample('D').last()
    
    daily_tr = (daily_high - daily_low).dropna()
    atr5 = daily_tr.rolling(5).mean()
    atr20 = daily_tr.rolling(20).mean()
    vol_ratio = (atr5 / atr20).fillna(1.0)
    
    # 2. Daily Gap
    daily_open = df['Open'].resample('D').first().dropna()
    prev_close = daily_close.shift(1).dropna()
    # Align indexes
    common_idx = daily_open.index.intersection(prev_close.index)
    gap_pct = (abs(daily_open.loc[common_idx] - prev_close.loc[common_idx]) / prev_close.loc[common_idx]).fillna(0)
    
    # 3. Decision Logic (Vectorized)
    regimes = pd.Series(MarketRegime.NEUTRAL, index=daily_tr.index)
    
    # SHOCK
    shock_mask = (gap_pct > 0.01) | (vol_ratio > 1.8)
    regimes.loc[shock_mask[shock_mask].index] = MarketRegime.SHOCK
    
    # TRENDING
    trend_mask = (~shock_mask) & ((vol_ratio > 1.05) | (gap_pct > 0.002))
    regimes.loc[trend_mask[trend_mask].index] = MarketRegime.TRENDING
    
    # CHOPPY
    chop_mask = (~shock_mask) & (~trend_mask) & (vol_ratio < 0.95)
    regimes.loc[chop_mask[chop_mask].index] = MarketRegime.CHOPPY
    
    return regimes

def classify_regime(df: pd.DataFrame) -> MarketRegime:
    """
    Legacy support: Classifies based on the tail of the DF.
    Warning: Needs at least 20 days of data to be accurate.
    """
    res = calculate_regimes(df)
    if not res.empty:
        return res.iloc[-1]
    return MarketRegime.NEUTRAL
