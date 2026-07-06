#!/usr/bin/env python3
"""
完整技術指標計算模組
為KbarFeatureStrategy提供所有必要特徵
"""

import pandas as pd
import numpy as np
import pandas_ta as ta
from typing import Dict, Any, Optional


class TechnicalIndicators:
    """
    技術指標計算器
    
    為KbarFeatureStrategy提供所有必要特徵：
    - adx (平均趨向指數)
    - macd_hist, macd_rising (MACD指標)
    - regime (市場狀態)
    - score (綜合分數)
    - bear_align, bull_align, bearish_align, bullish_align (對齊狀態)
    - mom_velo (動量速度)
    - recent_high, recent_low (近期高低點)
    - price_vs_vwap (價格vsVWAP)
    """
    
    def __init__(self, 
                 adx_period: int = 14,
                 macd_fast: int = 12,
                 macd_slow: int = 26,
                 macd_signal: int = 9,
                 ma_short: int = 20,
                 ma_long: int = 60,
                 recent_lookback: int = 20):
        """
        初始化技術指標計算器
        
        Args:
            adx_period: ADX計算週期
            macd_fast: MACD快速線週期
            macd_slow: MACD慢速線週期
            macd_signal: MACD信號線週期
            ma_short: 短期移動平均週期
            ma_long: 長期移動平均週期
            recent_lookback: 近期高低點回看週期
        """
        self.adx_period = adx_period
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.ma_short = ma_short
        self.ma_long = ma_long
        self.recent_lookback = recent_lookback
    
    def calculate_all(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        計算所有技術指標
        
        Args:
            df: 包含OHLCV數據的DataFrame
            
        Returns:
            包含所有技術指標的DataFrame
        """
        result = df.copy()
        
        # 1. 計算ADX (平均趨向指數)
        result = self._calculate_adx(result)
        
        # 2. 計算MACD
        result = self._calculate_macd(result)
        
        # 3. 計算移動平均和市場狀態
        result = self._calculate_ma_and_regime(result)
        
        # 4. 計算動量
        result = self._calculate_momentum(result)
        
        # 5. 計算近期高低點
        result = self._calculate_recent_high_low(result)
        
        # 6. 計算對齊狀態
        result = self._calculate_alignments(result)
        
        # 7. 計算綜合分數
        result = self._calculate_score(result)
        
        # 8. 計算價格vsVWAP
        if 'vwap' in result.columns:
            result['price_vs_vwap'] = (result['Close'] - result['vwap']) / result['vwap'] * 100
        
        return result
    
    def _calculate_adx(self, df: pd.DataFrame) -> pd.DataFrame:
        """計算ADX指標"""
        try:
            # 使用pandas_ta計算ADX
            adx_result = ta.adx(df['High'], df['Low'], df['Close'], length=self.adx_period)
            
            if adx_result is not None:
                # ADX指標
                df['adx'] = adx_result[f'ADX_{self.adx_period}']
                
                # +DI和-DI
                df['plus_di'] = adx_result[f'DMP_{self.adx_period}']
                df['minus_di'] = adx_result[f'DMN_{self.adx_period}']
            else:
                df['adx'] = 0.0
                df['plus_di'] = 0.0
                df['minus_di'] = 0.0
                
        except Exception as e:
            print(f"ADX計算錯誤: {e}")
            df['adx'] = 0.0
            df['plus_di'] = 0.0
            df['minus_di'] = 0.0
        
        return df
    
    def _calculate_macd(self, df: pd.DataFrame) -> pd.DataFrame:
        """計算MACD指標"""
        try:
            # 使用pandas_ta計算MACD
            macd_result = ta.macd(df['Close'], 
                                  fast=self.macd_fast, 
                                  slow=self.macd_slow, 
                                  signal=self.macd_signal)
            
            if macd_result is not None:
                # MACD線
                df['macd'] = macd_result[f'MACD_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}']
                
                # 信號線
                df['macd_signal'] = macd_result[f'MACDs_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}']
                
                # MACD柱狀圖
                df['macd_hist'] = macd_result[f'MACDh_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}']
                
                # MACD是否上升
                df['macd_rising'] = (df['macd_hist'] > df['macd_hist'].shift(1)).astype(int)
            else:
                df['macd'] = 0.0
                df['macd_signal'] = 0.0
                df['macd_hist'] = 0.0
                df['macd_rising'] = 0
                
        except Exception as e:
            print(f"MACD計算錯誤: {e}")
            df['macd'] = 0.0
            df['macd_signal'] = 0.0
            df['macd_hist'] = 0.0
            df['macd_rising'] = 0
        
        return df
    
    def _calculate_ma_and_regime(self, df: pd.DataFrame) -> pd.DataFrame:
        """計算移動平均和市場狀態"""
        # 計算移動平均
        df['ma_short'] = df['Close'].rolling(window=self.ma_short).mean()
        df['ma_long'] = df['Close'].rolling(window=self.ma_long).mean()
        
        # 計算市場狀態 (regime)
        # 1: 多頭市場 (短期MA > 長期MA)
        # 0: 震盪市場
        # -1: 空頭市場 (短期MA < 長期MA)
        df['regime'] = 0
        df.loc[df['ma_short'] > df['ma_long'], 'regime'] = 1
        df.loc[df['ma_short'] < df['ma_long'], 'regime'] = -1
        
        # 計算MA斜率
        df['ma_short_slope'] = df['ma_short'].diff()
        df['ma_long_slope'] = df['ma_long'].diff()
        
        return df
    
    def _calculate_momentum(self, df: pd.DataFrame) -> pd.DataFrame:
        """計算動量指標"""
        # 價格動量 (5期)
        df['momentum_5'] = df['Close'].pct_change(periods=5) * 100
        
        # 價格動量 (10期)
        df['momentum_10'] = df['Close'].pct_change(periods=10) * 100
        
        # 動量速度 (變化率)
        df['mom_velo'] = df['momentum_5'].diff()
        
        # RSI (相對強弱指數)
        try:
            rsi = ta.rsi(df['Close'], length=14)
            if rsi is not None:
                df['rsi'] = rsi
            else:
                df['rsi'] = 50.0
        except:
            df['rsi'] = 50.0
        
        return df
    
    def _calculate_recent_high_low(self, df: pd.DataFrame) -> pd.DataFrame:
        """計算近期高低點"""
        df['recent_high'] = df['High'].rolling(window=self.recent_lookback).max()
        df['recent_low'] = df['Low'].rolling(window=self.recent_lookback).min()
        
        # 計算距離近期高低點的百分比
        df['dist_to_recent_high'] = (df['Close'] - df['recent_high']) / df['recent_high'] * 100
        df['dist_to_recent_low'] = (df['Close'] - df['recent_low']) / df['recent_low'] * 100
        
        return df
    
    def _calculate_alignments(self, df: pd.DataFrame) -> pd.DataFrame:
        """計算對齊狀態"""
        # 多頭對齊: 價格 > 短期MA > 長期MA
        df['bull_align'] = ((df['Close'] > df['ma_short']) & 
                           (df['ma_short'] > df['ma_long'])).astype(int)
        
        # 空頭對齊: 價格 < 短期MA < 長期MA
        df['bear_align'] = ((df['Close'] < df['ma_short']) & 
                           (df['ma_short'] < df['ma_long'])).astype(int)
        
        # 看多對齊: 短期MA斜率 > 0 且 長期MA斜率 > 0
        df['bullish_align'] = ((df['ma_short_slope'] > 0) & 
                              (df['ma_long_slope'] > 0)).astype(int)
        
        # 看空對齊: 短期MA斜率 < 0 且 長期MA斜率 < 0
        df['bearish_align'] = ((df['ma_short_slope'] < 0) & 
                              (df['ma_long_slope'] < 0)).astype(int)
        
        return df
    
    def _calculate_score(self, df: pd.DataFrame) -> pd.DataFrame:
        """計算綜合分數"""
        # 初始化分數
        df['score'] = 0.0
        
        # 1. 趨勢分數 (基於regime)
        df['score'] += df['regime'] * 10
        
        # 2. 動量分數 (基於momentum_5)
        df['score'] += np.clip(df['momentum_5'] / 5, -10, 10)
        
        # 3. ADX分數 (趨勢強度)
        if 'adx' in df.columns:
            df['score'] += np.where(df['adx'] > 25, 5, 0)
            df['score'] += np.where(df['adx'] > 40, 5, 0)
        
        # 4. RSI分數
        if 'rsi' in df.columns:
            # RSI超買超賣
            df['score'] += np.where(df['rsi'] > 70, -5, 0)
            df['score'] += np.where(df['rsi'] < 30, 5, 0)
        
        # 5. MACD分數
        if 'macd_hist' in df.columns:
            df['score'] += np.where(df['macd_hist'] > 0, 3, -3)
            df['score'] += np.where(df['macd_rising'] == 1, 2, -2)
        
        # 6. 成交量分數
        if 'volume_spike' in df.columns:
            df['score'] += np.where(df['volume_spike'] > 1.5, 3, 0)
        
        # 7. 對齊分數
        df['score'] += df['bull_align'] * 5
        df['score'] += df['bear_align'] * -5
        df['score'] += df['bullish_align'] * 3
        df['score'] += df['bearish_align'] * -3
        
        # 正規化分數到-100到100範圍
        df['score'] = np.clip(df['score'], -100, 100)
        
        return df


def test_technical_indicators():
    """測試技術指標計算"""
    print("🧪 測試技術指標計算模組...")
    
    # 讀取清洗後數據
    cleaned_path = "data/cleaned_final/STOCK_2330_5m_cleaned.csv"
    df = pd.read_csv(cleaned_path)
    
    # 設置索引
    if 'Unnamed: 0' in df.columns:
        df = df.set_index('Unnamed: 0')
        df.index = pd.to_datetime(df.index)
        df.index.name = 'timestamp'
    
    print(f"📊 測試數據:")
    print(f"  筆數: {len(df)}")
    print(f"  時間範圍: {df.index.min()} 到 {df.index.max()}")
    
    # 創建技術指標計算器
    ti = TechnicalIndicators(
        adx_period=14,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        ma_short=20,
        ma_long=60,
        recent_lookback=20
    )
    
    # 計算所有技術指標
    print(f"\n🧮 計算技術指標...")
    df_with_indicators = ti.calculate_all(df)
    
    print(f"📈 技術指標計算完成:")
    print(f"  原始欄位: {len(df.columns)} 個")
    print(f"  技術指標欄位: {len(df_with_indicators.columns)} 個")
    
    # 檢查KbarFeatureStrategy所需特徵
    print(f"\n🎯 檢查KbarFeatureStrategy.REQUIRED_COLUMNS:")
    
    required_columns = {
        "close", "high", "low", "atr", "vwap", "adx", "score",
        "regime", "bear_align", "bull_align", "bearish_align", "bullish_align",
        "macd_hist", "macd_rising", "mom_velo", "recent_high", "recent_low",
        "price_vs_vwap", "volume_spike",
    }
    
    existing_columns = set(df_with_indicators.columns)
    missing = required_columns - existing_columns
    
    if missing:
        print(f"❌ 缺少欄位: {sorted(missing)}")
    else:
        print(f"✅ 所有必要欄位都存在")
    
    # 檢查每個特徵的數據品質
    print(f"\n🔍 技術指標數據品質:")
    
    for col in sorted(required_columns):
        if col in df_with_indicators.columns:
            series = df_with_indicators[col]
            non_nan = series.notna().sum()
            if non_nan > 0:
                print(f"  {col}: {non_nan}筆有效 ({non_nan/len(series)*100:.1f}%)")
            else:
                print(f"  {col}: ⚠️ 全部為NaN")
        else:
            print(f"  {col}: ❌ 欄位不存在")
    
    # 保存結果
    output_path = "data/technical_indicators/STOCK_2330_5m_with_indicators.csv"
    import os
    os.makedirs("data/technical_indicators", exist_ok=True)
    
    df_with_indicators.to_csv(output_path)
    print(f"\n💾 保存技術指標數據: {output_path}")
    
    # 顯示一些樣本數據
    print(f"\n📊 樣本數據 (最後5行):")
    sample_cols = ['Close', 'adx', 'macd_hist', 'regime', 'score', 'volume_spike']
    sample_cols = [col for col in sample_cols if col in df_with_indicators.columns]
    
    if sample_cols:
        print(df_with_indicators[sample_cols].tail())
    
    return df_with_indicators


if __name__ == "__main__":
    test_technical_indicators()
