#!/usr/bin/env python3
"""
增強版DataEnricher - 添加頻率檢測和動態窗口調整
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
import numpy as np


def detect_data_frequency(df: pd.DataFrame) -> Dict[str, Any]:
    """
    檢測數據頻率
    
    Returns:
        Dict containing frequency information
    """
    if len(df) < 2:
        return {'detected': 'unknown', 'window_multiplier': 1.0}
    
    # 計算時間間隔
    time_diffs = df.index.to_series().diff().dropna()
    
    if len(time_diffs) == 0:
        return {'detected': 'unknown', 'window_multiplier': 1.0}
    
    # 找到主要間隔
    primary_diff = time_diffs.mode()[0]
    
    # 判斷頻率類型並設置窗口乘數
    if primary_diff >= pd.Timedelta(days=1):
        detected = 'daily'
        window_multiplier = 1.0  # 日線使用標準窗口
    elif primary_diff >= pd.Timedelta(hours=1):
        detected = 'hourly'
        window_multiplier = 12.0  # 小時數據，20窗口 = 20小時 ≈ 3天
    elif primary_diff >= pd.Timedelta(minutes=5):
        detected = '5min'
        window_multiplier = 48.0  # 5分鐘數據，20窗口 = 100分鐘 ≈ 1.7小時
    elif primary_diff >= pd.Timedelta(minutes=1):
        detected = '1min'
        window_multiplier = 240.0  # 1分鐘數據，20窗口 = 20分鐘
    else:
        detected = 'tick'
        window_multiplier = 1.0
    
    return {
        'detected': detected,
        'primary_interval': primary_diff,
        'window_multiplier': window_multiplier,
        'unique_intervals': len(time_diffs.unique())
    }


def _calc_atr_enhanced(df: pd.DataFrame, freq_info: Dict[str, Any], **kwargs) -> pd.DataFrame:
    """增強版ATR計算，考慮數據頻率"""
    length = int(kwargs.get("atr_length", 14))
    
    # 根據頻率調整ATR長度
    window_multiplier = freq_info.get('window_multiplier', 1.0)
    adjusted_length = int(length * window_multiplier)
    
    high, low, close = df["High"].values, df["Low"].values, df["Close"].values
    
    tr = np.zeros(len(close))
    tr[0] = high[0] - low[0]
    tr[1:] = np.maximum(high[1:] - low[1:], 
                        np.maximum(np.abs(high[1:] - close[:-1]), 
                                   np.abs(low[1:] - close[:-1])))
    
    atr = pd.Series(tr).rolling(window=adjusted_length).mean().values
    df = df.copy()
    df["atr"] = atr
    return df


def _calc_vwap_enhanced(df: pd.DataFrame, freq_info: Dict[str, Any], **kwargs) -> pd.DataFrame:
    """增強版VWAP計算"""
    v, p = df["Volume"].values, (df["High"].values + df["Low"].values + df["Close"].values) / 3
    df = df.copy()
    
    # 根據頻率調整計算方式
    if freq_info['detected'] == 'daily':
        # 日線數據：簡單累積
        df["vwap"] = np.cumsum(p * v) / np.cumsum(v)
    else:
        # 分鐘數據：按交易日分組
        if "trading_day" in df.columns:
            pv = pd.Series(p * v, index=df.index)
            vol_ser = pd.Series(v, index=df.index)
            cum_pv = pv.groupby(df["trading_day"]).cumsum()
            cum_v = vol_ser.groupby(df["trading_day"]).cumsum()
            df["vwap"] = cum_pv / cum_v
        else:
            # 如果沒有交易日欄位，創建一個（假設每天數據連續）
            df["vwap"] = np.cumsum(p * v) / np.cumsum(v)
    
    return df


def _calc_alpha_features_enhanced(df: pd.DataFrame, freq_info: Dict[str, Any], **kwargs) -> pd.DataFrame:
    """
    增強版Alpha特徵計算
    
    根據數據頻率動態調整計算窗口
    """
    res = df.copy()
    window_multiplier = freq_info.get('window_multiplier', 1.0)
    
    # 1. 根據頻率調整窗口大小
    base_window_high_low = 20  # 基礎窗口
    base_window_ma_short = 20
    base_window_ma_long = 60
    base_window_volume = 20
    
    adjusted_window_high_low = int(base_window_high_low * window_multiplier)
    adjusted_window_ma_short = int(base_window_ma_short * window_multiplier)
    adjusted_window_ma_long = int(base_window_ma_long * window_multiplier)
    adjusted_window_volume = int(base_window_volume * window_multiplier)
    
    print(f"  頻率調整: {freq_info['detected']}, 窗口乘數: {window_multiplier:.1f}")
    print(f"  調整後窗口: 高點={adjusted_window_high_low}, MA短={adjusted_window_ma_short}, MA長={adjusted_window_ma_long}")
    
    # 2. Breakout Strength (Price relative to recent range)
    high_n = res["High"].rolling(adjusted_window_high_low).max().shift(1)
    low_n = res["Low"].rolling(adjusted_window_high_low).min().shift(1)
    
    # 確保ATR存在
    if "atr" not in res.columns:
        res = _calc_atr_enhanced(res, freq_info)
    atr = res["atr"]
    
    res["breakout_strength"] = (res["Close"] - high_n) / atr.replace(0, np.nan)
    
    # 3. Volume Spike (Relative volume)
    vol_avg = res["Volume"].rolling(adjusted_window_volume).mean()
    res["volume_spike"] = res["Volume"] / vol_avg.replace(0, np.nan)
    
    # 4. Normalized VWAP Distance
    if "vwap" not in res.columns:
        res = _calc_vwap_enhanced(res, freq_info)
        
    res["vwap_dist_norm"] = (res["Close"] - res["vwap"]) / atr.replace(0, np.nan)
    
    # 5. Trend Structure (MA alignment)
    ma_short = res["Close"].rolling(adjusted_window_ma_short).mean()
    ma_long = res["Close"].rolling(adjusted_window_ma_long).mean()
    res["trend_strength_raw"] = (ma_short - ma_long) / res["Close"]
    
    # 6. [GSD 4.5] Interaction Features
    # A. Trend-Volatility Clash
    res["trend_vol_interaction"] = res["trend_strength_raw"] * (res["atr"] / res["Close"])
    
    # B. Signal-Volume Sync
    if "momentum" in res.columns:
        res["signal_vol_sync"] = np.sign(res["momentum"]) * res["volume_spike"]
    
    # C. Range Position (0 to 1 scaling within recent high/low)
    res["range_pos"] = (res["Close"] - low_n) / (high_n - low_n).replace(0, np.nan)
    
    return res


class DataEnricherEnhanced:
    """
    增強版DataEnricher - 支持頻率自適應特徵計算
    """
    
    def __init__(self, logger: Optional[logging.Logger] = None):
        self.registry: Dict[str, Callable] = {}
        self.logger = logger or logging.getLogger(__name__)
        self.freq_info: Dict[str, Any] = {}
        
        # 註冊增強版計算函數
        self.register("atr", _calc_atr_enhanced)
        self.register("vwap", _calc_vwap_enhanced)
        self.register("alpha", _calc_alpha_features_enhanced)
    
    def register(self, name: str, func: Callable) -> None:
        """註冊計算函數"""
        self.registry[name] = func
    
    def detect_and_set_frequency(self, df: pd.DataFrame) -> None:
        """檢測並設置數據頻率"""
        self.freq_info = detect_data_frequency(df)
        if self.logger:
            self.logger.info(f"檢測到數據頻率: {self.freq_info}")
    
    def enrich(self, df: pd.DataFrame, indicators: Optional[List[str]] = None, **kwargs) -> pd.DataFrame:
        """
        增強版特徵計算
        
        Args:
            df: 輸入數據
            indicators: 要計算的指標列表，如果為None則計算所有
            **kwargs: 傳遞給計算函數的參數
            
        Returns:
            特徵豐富的數據
        """
        # 檢測數據頻率
        self.detect_and_set_frequency(df)
        
        print(f"🔍 數據頻率檢測結果:")
        print(f"  類型: {self.freq_info.get('detected', 'unknown')}")
        print(f"  主要間隔: {self.freq_info.get('primary_interval', 'unknown')}")
        print(f"  窗口乘數: {self.freq_info.get('window_multiplier', 1.0):.1f}")
        
        # 如果沒有指定指標，計算所有註冊的指標
        if indicators is None:
            indicators = list(self.registry.keys())
        
        result = df.copy()
        
        for indicator in indicators:
            if indicator in self.registry:
                try:
                    print(f"🧮 計算指標: {indicator}")
                    func = self.registry[indicator]
                    # 傳遞頻率信息給計算函數
                    result = func(result, self.freq_info, **kwargs)
                except Exception as e:
                    if self.logger:
                        self.logger.error(f"計算指標 {indicator} 失敗: {e}")
                    raise
            else:
                if self.logger:
                    self.logger.warning(f"未知指標: {indicator}")
        
        return result


def test_enhanced_enricher():
    """測試增強版DataEnricher"""
    print("🧪 測試增強版DataEnricher...")
    
    # 讀取清洗後數據
    cleaned_path = "data/cleaned_fixed/STOCK_2330_5m_cleaned.csv"
    df = pd.read_csv(cleaned_path)
    
    # 設置索引
    if 'Unnamed: 0' in df.columns:
        df = df.set_index('Unnamed: 0')
        df.index = pd.to_datetime(df.index)
        df.index.name = 'timestamp'
    
    print(f"📊 測試數據:")
    print(f"  筆數: {len(df)}")
    print(f"  時間範圍: {df.index.min()} 到 {df.index.max()}")
    
    # 創建增強版DataEnricher
    enricher = DataEnricherEnhanced()
    
    # 計算特徵
    print(f"\n🧮 開始計算特徵...")
    enriched_df = enricher.enrich(df, indicators=['atr', 'vwap', 'alpha'])
    
    print(f"\n📈 特徵計算完成:")
    print(f"  原始欄位: {len(df.columns)} 個")
    print(f"  特徵欄位: {len(enriched_df.columns)} 個")
    
    # 檢查關鍵特徵
    print(f"\n🔍 關鍵特徵統計:")
    
    key_features = ['volume_spike', 'breakout_strength', 'trend_strength_raw', 'atr', 'vwap']
    
    for feature in key_features:
        if feature in enriched_df.columns:
            series = enriched_df[feature]
            non_nan = series.notna().sum()
            if non_nan > 0:
                print(f"\n  {feature}:")
                print(f"    非NaN值: {non_nan}/{len(series)}")
                print(f"    平均值: {series.mean():.6f}")
                print(f"    標準差: {series.std():.6f}")
                print(f"    範圍: {series.min():.6f} - {series.max():.6f}")
            else:
                print(f"\n  {feature}: 全部為NaN")
        else:
            print(f"\n  {feature}: 欄位不存在")
    
    # 檢查策略所需特徵
    print(f"\n🎯 檢查策略所需特徵 (KbarFeatureStrategy.REQUIRED_COLUMNS):")
    required_columns = {
        "close", "high", "low", "atr", "vwap", "adx", "score",
        "regime", "bear_align", "bull_align", "bearish_align", "bullish_align",
        "macd_hist", "macd_rising", "mom_velo", "recent_high", "recent_low",
        "price_vs_vwap", "volume_spike",
    }
    
    existing_columns = set(enriched_df.columns.str.lower())
    missing = required_columns - existing_columns
    
    if missing:
        print(f"❌ 缺少必要欄位: {sorted(missing)}")
    else:
        print(f"✅ 所有必要欄位都存在")
    
    print(f"\n💡 結論:")
    print(f"   增強版DataEnricher能根據數據頻率動態調整計算")
    print(f"   特徵計算更適合清洗後的數據")
    
    return enriched_df


if __name__ == "__main__":
    test_enhanced_enricher()
