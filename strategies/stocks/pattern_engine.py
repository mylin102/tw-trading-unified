"""
Pattern Recognition Engine for Taiwan Stocks.
Implements geometric pattern detection like Cup with Handle and Double Bottom.
Inspired by CANSLIM methodology and optimized for automated scanning.
"""
import pandas as pd
import numpy as np
from scipy.signal import argrelextrema
from typing import Tuple, Optional, Dict

def detect_cup_with_handle(
    df: pd.DataFrame, 
    cup_depth_min: float = 0.12, 
    cup_depth_max: float = 0.35, 
    handle_depth_max: float = 0.15,
    order: int = 5
) -> Dict:
    """
    Detects a Cup with Handle pattern in the provided daily OHLCV data.
    """
    if df.empty or len(df) < 60:
        return {"status": False, "reason": "insufficient_data"}

    # 1. Smoothing to find "structural" peaks
    prices_raw = df['Close']
    prices_smooth = prices_raw.rolling(window=5, center=True).mean().bfill().ffill()
    
    # 2. Find local maxima on smoothed data
    maxima_idx = argrelextrema(prices_smooth.values, np.greater, order=order)[0]
    
    if len(maxima_idx) < 1:
        # Fallback: search for absolute high in lookback
        lookback = min(200, len(df))
        relevant_df = df.iloc[-lookback:]
        # Use simple idxmax if no local maxima found
        # Left lip must be at least 30 days ago to allow for cup + handle
        if len(relevant_df) < 40:
            return {"status": False, "reason": "insufficient_lookback"}
        potential_left_lip_idx_num = relevant_df['High'].iloc[:-30].argmax()
        potential_left_lip_idx = relevant_df.index[potential_left_lip_idx_num]
    else:
        # Use the most significant peak from argrelextrema as potential left lip
        # Must be at least 30 days ago to allow for cup + handle
        valid_peaks = [idx for idx in maxima_idx if idx < len(df) - 30]
        if not valid_peaks:
            # Fallback to absolute high if extrema peaks are too recent
            lookback = min(200, len(df))
            potential_left_lip_idx_num = df['High'].iloc[-lookback:-30].argmax()
            potential_left_lip_idx = df.index[len(df) - lookback + potential_left_lip_idx_num]
        else:
            # Pick the highest of valid peaks
            left_lip_idx_num = valid_peaks[np.argmax(prices_smooth.values[valid_peaks])]
            potential_left_lip_idx = df.index[left_lip_idx_num]

    left_lip_price = df.loc[potential_left_lip_idx, 'High']
    relevant_df = df.loc[potential_left_lip_idx:]
    
    # 2. Find the lowest point after the left lip
    after_left_lip = relevant_df
    if len(after_left_lip) < 30:
        return {"status": False, "reason": "cup_too_short"}

    # Reserve room for the handle so a deep handle low is not misclassified as
    # the cup bottom. This preserves the intended rule ordering: cup -> recovery -> handle.
    handle_buffer_bars = max(10, order * 4)
    bottom_search_df = after_left_lip.iloc[:-handle_buffer_bars] if len(after_left_lip) > handle_buffer_bars else after_left_lip
    if len(bottom_search_df) < 10:
        return {"status": False, "reason": "cup_too_short"}

    bottom_price = bottom_search_df['Low'].min()
    bottom_idx = bottom_search_df['Low'].idxmin()
    depth = (left_lip_price - bottom_price) / left_lip_price
    
    if depth < cup_depth_min or depth > cup_depth_max:
        return {"status": False, "reason": f"invalid_cup_depth: {depth:.2f}"}

    # 3. Find the right lip (recovery towards left lip price)
    # MUST be after the bottom. Search backward but leave room for handle (min 1d)
    after_bottom = relevant_df.loc[bottom_idx:]
    if len(after_bottom) < 5:
        return {"status": False, "reason": "recovery_too_short"}

    # Search for recovery, but stop at least 1 day before the end
    search_df = after_bottom.iloc[:-1]
    # Recovery: price within 20% of left lip
    recovery_zone = search_df[search_df['High'] >= left_lip_price * 0.80]
    
    if recovery_zone.empty:
        return {"status": False, "reason": "incomplete_recovery"}
        
    # Pick the point with the HIGHEST price in the recovery zone as the Right Lip
    right_lip_idx = recovery_zone['High'].idxmax()
    right_lip_price = recovery_zone.loc[right_lip_idx, 'High']
    
    # GSD: If right_lip is too close to today, handle hasn't formed
    days_left = (relevant_df.index[-1] - right_lip_idx).days
    if days_left < 3:
        return {"status": False, "reason": f"handle_too_short: {days_left}d"}
    
    # 4. Detect handle (shallow consolidation after right lip)
    handle_df = df.loc[right_lip_idx:]
    if len(handle_df) < 3:
        return {"status": False, "reason": "handle_df_too_short"}
        
    handle_max = handle_df['High'].max()
    handle_min = handle_df['Low'].min()
    # Handle depth should be calculated relative to right lip
    handle_depth = (right_lip_price - handle_min) / right_lip_price
    
    if handle_depth > handle_depth_max:
        return {"status": False, "reason": f"handle_too_deep: {handle_depth:.2f} > {handle_depth_max}"}
        
    # Pivot point is usually the high of the handle
    pivot_price = handle_max
    
    return {
        "status": True,
        "type": "cup_with_handle",
        "pivot_price": pivot_price,
        "left_lip_price": left_lip_price,
        "bottom_price": bottom_price,
        "depth": depth,
        "handle_depth": handle_depth,
        "base_start": potential_left_lip_idx,
        "right_lip": right_lip_idx
    }

def detect_double_bottom(df: pd.DataFrame, tolerance: float = 0.05) -> Dict:
    """Detects a Double Bottom (W) pattern."""
    # Placeholder for Wave 2
    return {"status": False, "reason": "not_implemented"}
