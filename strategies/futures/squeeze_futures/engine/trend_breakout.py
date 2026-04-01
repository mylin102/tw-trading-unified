#!/usr/bin/env python3
"""
趨勢突破指標模組

功能：
1. 繪製高點/低點連線趨勢線
2. 計算均線斜率 (當前 vs N 根前)
3. 判斷趨勢突破信號
"""

import numpy as np
import pandas as pd
from typing import Tuple, Optional


def calculate_trend_line(
    df: pd.DataFrame,
    lookback: int = 20,
    min_touches: int = 2
) -> Tuple[Optional[float], Optional[float], str]:
    """
    計算趨勢線 (高點/低點連線)
    
    Args:
        df: OHLCV 數據
        lookback: 回顧週期
        min_touches: 最少接觸點數
    
    Returns:
        (多頭趨勢線值，空頭趨勢線值，趨勢方向)
    """
    if len(df) < lookback:
        return None, None, "UNKNOWN"
    
    recent = df.tail(lookback)
    
    # 尋找顯著高點和低點
    high_points = []
    low_points = []
    
    for i in range(2, len(recent) - 2):
        # 檢查是否為局部高點
        if (recent['High'].iloc[i] > recent['High'].iloc[i-1] and
            recent['High'].iloc[i] > recent['High'].iloc[i-2] and
            recent['High'].iloc[i] > recent['High'].iloc[i+1] and
            recent['High'].iloc[i] > recent['High'].iloc[i+2]):
            high_points.append((i, recent['High'].iloc[i]))
        
        # 檢查是否為局部低點
        if (recent['Low'].iloc[i] < recent['Low'].iloc[i-1] and
            recent['Low'].iloc[i] < recent['Low'].iloc[i-2] and
            recent['Low'].iloc[i] < recent['Low'].iloc[i+1] and
            recent['Low'].iloc[i] < recent['Low'].iloc[i+2]):
            low_points.append((i, recent['Low'].iloc[i]))
    
    # 計算多頭趨勢線 (連接低點)
    bull_trend_line = None
    if len(low_points) >= min_touches:
        # 使用最近兩個低點
        p1, p2 = low_points[-2], low_points[-1]
        slope = (p2[1] - p1[1]) / (p2[0] - p1[0])
        # 延伸趨勢線到當前
        current_idx = len(recent) - 1
        bull_trend_line = p2[1] + slope * (current_idx - p2[0])
    
    # 計算空頭趨勢線 (連接高點)
    bear_trend_line = None
    if len(high_points) >= min_touches:
        # 使用最近兩個高點
        p1, p2 = high_points[-2], high_points[-1]
        slope = (p2[1] - p1[1]) / (p2[0] - p1[0])
        # 延伸趨勢線到當前
        current_idx = len(recent) - 1
        bear_trend_line = p2[1] + slope * (current_idx - p2[0])
    
    # 判斷趨勢方向
    current_price = df['Close'].iloc[-1]
    trend_direction = "UNKNOWN"
    
    if bull_trend_line and current_price > bull_trend_line:
        trend_direction = "BULLISH"
    elif bear_trend_line and current_price < bear_trend_line:
        trend_direction = "BEARISH"
    
    return bull_trend_line, bear_trend_line, trend_direction


def calculate_ma_slope(
    df: pd.DataFrame,
    ma_length: int = 20,
    compare_bars: int = 5
) -> float:
    """
    計算均線斜率 (當前 vs N 根前)
    
    Args:
        df: OHLCV 數據
        ma_length: MA 週期
        compare_bars: 比較的 K 棒數
    
    Returns:
        斜率值 (正=向上，負=向下)
    """
    if len(df) < ma_length + compare_bars:
        return 0.0
    
    # 計算 MA
    ma = df['Close'].rolling(window=ma_length).mean()
    
    # 當前 MA 值
    current_ma = ma.iloc[-1]
    
    # N 根前的 MA 值
    past_ma = ma.iloc[-compare_bars - 1]
    
    # 計算斜率 (百分比變化)
    if past_ma == 0:
        return 0.0
    
    slope = (current_ma - past_ma) / past_ma * 100
    
    return slope


def check_trend_breakout(
    df: pd.DataFrame,
    lookback: int = 20,
    ma_length: int = 20,
    compare_bars: int = 5,
    slope_threshold: float = 0.1
) -> dict:
    """
    檢查趨勢突破信號
    
    Args:
        df: OHLCV 數據
        lookback: 趨勢線回顧週期
        ma_length: MA 週期
        compare_bars: 斜率比較 K 棒數
        slope_threshold: 斜率門檻 (%)
    
    Returns:
        信號字典
    """
    current_price = df['Close'].iloc[-1]
    
    # 計算趨勢線
    bull_line, bear_line, trend_dir = calculate_trend_line(df, lookback)
    
    # 計算 MA 斜率
    ma_slope = calculate_ma_slope(df, ma_length, compare_bars)
    
    # 判斷信號
    long_signal = False
    short_signal = False
    long_reason = []
    short_reason = []
    
    # 多頭信號：價格 > 多頭趨勢線 + MA 斜率 > 門檻
    if bull_line and current_price > bull_line:
        long_reason.append(f"Price ({current_price:.0f}) > Bull Trend ({bull_line:.0f})")
    
    if ma_slope > slope_threshold:
        long_reason.append(f"MA Slope ({ma_slope:.2f}%) > Threshold ({slope_threshold}%)")
    
    if bull_line and current_price > bull_line and ma_slope > slope_threshold:
        long_signal = True
    
    # 空頭信號：價格 < 空頭趨勢線 + MA 斜率 < -門檻
    if bear_line and current_price < bear_line:
        short_reason.append(f"Price ({current_price:.0f}) < Bear Trend ({bear_line:.0f})")
    
    if ma_slope < -slope_threshold:
        short_reason.append(f"MA Slope ({ma_slope:.2f}%) < -Threshold ({-slope_threshold}%)")
    
    if bear_line and current_price < bear_line and ma_slope < -slope_threshold:
        short_signal = True
    
    return {
        'long_signal': long_signal,
        'short_signal': short_signal,
        'long_reasons': long_reason,
        'short_reasons': short_reason,
        'bull_trend_line': bull_line,
        'bear_trend_line': bear_line,
        'trend_direction': trend_dir,
        'ma_slope': ma_slope,
        'current_price': current_price,
    }


def add_trend_indicators(
    df: pd.DataFrame,
    lookback: int = 20,
    ma_length: int = 20,
    compare_bars: int = 5
) -> pd.DataFrame:
    """
    添加趨勢指標到 DataFrame
    
    Args:
        df: OHLCV 數據
        lookback: 趨勢線回顧週期
        ma_length: MA 週期
        compare_bars: 斜率比較 K 棒數
    
    Returns:
        添加指標後的 DataFrame
    """
    result = df.copy()
    
    # 計算 MA
    result['ma_20'] = result['Close'].rolling(window=20).mean()
    result['ma_60'] = result['Close'].rolling(window=60).mean()
    
    # 計算 MA 斜率
    result['ma_slope_20'] = result['ma_20'].pct_change(periods=compare_bars) * 100
    result['ma_slope_60'] = result['ma_60'].pct_change(periods=compare_bars) * 100
    
    # 計算趨勢線 (滾動計算)
    bull_lines = []
    bear_lines = []
    trend_dirs = []
    
    for i in range(len(result)):
        if i < lookback:
            bull_lines.append(None)
            bear_lines.append(None)
            trend_dirs.append("UNKNOWN")
        else:
            bull, bear, direction = calculate_trend_line(result.iloc[:i+1], lookback)
            bull_lines.append(bull)
            bear_lines.append(bear)
            trend_dirs.append(direction)
    
    result['bull_trend_line'] = bull_lines
    result['bear_trend_line'] = bear_lines
    result['trend_direction'] = trend_dirs
    
    return result
