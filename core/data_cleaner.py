#!/usr/bin/env python3
"""
數據清洗模組 - 清理混合頻率數據，統一為5分鐘頻率
"""

import pandas as pd
import numpy as np
from typing import Optional, Dict, Any
from datetime import time, datetime


class DataCleaner:
    """
    數據清洗器：處理混合頻率股票數據
    
    主要功能：
    1. 檢測數據頻率
    2. 過濾正常交易時段
    3. 重採樣為統一頻率
    4. 填充缺失值
    5. 驗證數據品質
    """
    
    # 台灣股票市場交易時間
    TRADING_HOURS = {
        'regular': {
            'start': time(9, 0),   # 09:00
            'end': time(13, 30)    # 13:30
        }
    }
    
    def __init__(self, target_freq: str = '5min'):
        """
        初始化數據清洗器
        
        Args:
            target_freq: 目標頻率，預設為5分鐘
        """
        self.target_freq = target_freq
        
    def detect_frequency(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        檢測數據頻率
        
        Returns:
            Dict containing frequency analysis
        """
        if len(df) < 2:
            return {'detected': 'unknown', 'confidence': 0.0}
        
        # 計算時間間隔
        time_diffs = df.index.to_series().diff().dropna()
        
        if len(time_diffs) == 0:
            return {'detected': 'unknown', 'confidence': 0.0}
        
        # 統計不同間隔的出現頻率
        diff_counts = time_diffs.value_counts()
        total_diffs = len(time_diffs)
        
        # 識別主要頻率
        primary_diff = diff_counts.index[0]
        primary_count = diff_counts.iloc[0]
        primary_ratio = primary_count / total_diffs
        
        # 判斷頻率類型
        if primary_diff >= pd.Timedelta(days=1):
            detected = 'daily'
        elif primary_diff >= pd.Timedelta(hours=1):
            detected = 'hourly'
        elif primary_diff >= pd.Timedelta(minutes=5):
            detected = '5min'
        elif primary_diff >= pd.Timedelta(minutes=1):
            detected = '1min'
        else:
            detected = 'tick'
        
        return {
            'detected': detected,
            'confidence': primary_ratio,
            'primary_interval': primary_diff,
            'interval_distribution': diff_counts.head(10).to_dict(),
            'total_intervals': total_diffs,
            'unique_intervals': len(diff_counts)
        }
    
    def filter_trading_hours(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        過濾出正常交易時段的數據
        
        Args:
            df: 原始數據
            
        Returns:
            過濾後的數據
        """
        if len(df) == 0:
            return df
        
        # 提取時間部分
        time_mask = df.index.to_series().apply(
            lambda x: self.TRADING_HOURS['regular']['start'] <= x.time() <= self.TRADING_HOURS['regular']['end']
        )
        
        filtered_df = df[time_mask]
        
        print(f"  交易時段過濾: {len(df)} → {len(filtered_df)} 筆")
        return filtered_df
    
    def resample_to_frequency(self, df: pd.DataFrame, freq: Optional[str] = None) -> pd.DataFrame:
        """
        重採樣為目標頻率
        
        Args:
            df: 原始數據
            freq: 目標頻率，如果為None則使用self.target_freq
            
        Returns:
            重採樣後的數據
        """
        if len(df) == 0:
            return df
        
        target_freq = freq or self.target_freq
        
        # 確保索引是DatetimeIndex
        if not isinstance(df.index, pd.DatetimeIndex):
            df = df.copy()
            df.index = pd.DatetimeIndex(df.index)
        
        # 重採樣
        resampled = df.resample(target_freq).agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum'
        })
        
        print(f"  重採樣為 {target_freq}: {len(df)} → {len(resampled)} 筆")
        return resampled
    
    def fill_missing_values(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        填充缺失值
        
        Args:
            df: 數據
            
        Returns:
            填充缺失值後的數據
        """
        if len(df) == 0:
            return df
        
        df_filled = df.copy()
        
        # 前向填充價格數據
        price_cols = ['Open', 'High', 'Low', 'Close']
        for col in price_cols:
            if col in df_filled.columns:
                df_filled[col] = df_filled[col].ffill()
        
        # 成交量用0填充
        if 'Volume' in df_filled.columns:
            df_filled['Volume'] = df_filled['Volume'].fillna(0)
        
        # 檢查填充結果
        missing_before = df.isna().sum().sum()
        missing_after = df_filled.isna().sum().sum()
        
        if missing_after > 0:
            print(f"  ⚠️ 仍有缺失值: {missing_after}")
        else:
            print(f"  ✅ 所有缺失值已填充")
        
        return df_filled
    
    def validate_data_quality(self, df: pd.DataFrame) -> Dict[str, Any]:
        """
        驗證數據品質
        
        Returns:
            品質驗證報告
        """
        report = {
            'total_rows': len(df),
            'issues': []
        }
        
        # 1. 檢查基本欄位
        required_cols = ['Open', 'High', 'Low', 'Close', 'Volume']
        missing_cols = [col for col in required_cols if col not in df.columns]
        if missing_cols:
            report['issues'].append(f"缺少欄位: {missing_cols}")
        
        # 2. 檢查缺失值
        for col in required_cols:
            if col in df.columns:
                missing = df[col].isna().sum()
                if missing > 0:
                    report['issues'].append(f"{col} 有 {missing} 個缺失值")
        
        # 3. 檢查價格合理性
        if all(col in df.columns for col in ['Open', 'High', 'Low', 'Close']):
            price_issues = 0
            for idx, row in df.iterrows():
                if not (row['Low'] <= row['Open'] <= row['High'] and 
                        row['Low'] <= row['Close'] <= row['High']):
                    price_issues += 1
            if price_issues > 0:
                report['issues'].append(f"價格異常: {price_issues} 筆")
        
        # 4. 檢查成交量
        if 'Volume' in df.columns:
            zero_volume = (df['Volume'] == 0).sum()
            if zero_volume > 0:
                report['issues'].append(f"成交量為0: {zero_volume} 筆")
            
            negative_volume = (df['Volume'] < 0).sum()
            if negative_volume > 0:
                report['issues'].append(f"成交量為負: {negative_volume} 筆")
        
        # 5. 檢查時間連續性
        if len(df) > 1:
            time_diffs = df.index.to_series().diff().dropna()
            if len(time_diffs) > 0:
                expected_freq = pd.Timedelta(self.target_freq)
                freq_issues = (time_diffs != expected_freq).sum()
                if freq_issues > 0:
                    report['issues'].append(f"時間間隔不一致: {freq_issues} 處")
        
        report['has_issues'] = len(report['issues']) > 0
        report['issue_count'] = len(report['issues'])
        
        return report
    
    def clean(self, df: pd.DataFrame, verbose: bool = True) -> pd.DataFrame:
        """
        完整數據清洗流程
        
        Args:
            df: 原始數據
            verbose: 是否顯示詳細資訊
            
        Returns:
            清洗後的數據
        """
        if verbose:
            print("🧹 開始數據清洗流程...")
            print(f"  原始數據: {len(df)} 筆")
        
        # 1. 檢測頻率
        freq_info = self.detect_frequency(df)
        if verbose:
            print(f"  檢測頻率: {freq_info['detected']} (信心度: {freq_info['confidence']:.1%})")
        
        # 2. 過濾交易時段
        filtered_df = self.filter_trading_hours(df)
        
        # 3. 重採樣為目標頻率
        resampled_df = self.resample_to_frequency(filtered_df)
        
        # 4. 填充缺失值
        filled_df = self.fill_missing_values(resampled_df)
        
        # 5. 驗證數據品質
        quality_report = self.validate_data_quality(filled_df)
        
        if verbose:
            print(f"  清洗後數據: {len(filled_df)} 筆")
            if quality_report['has_issues']:
                print(f"  ⚠️ 數據品質問題: {quality_report['issue_count']} 個")
                for issue in quality_report['issues']:
                    print(f"    - {issue}")
            else:
                print(f"  ✅ 數據品質良好")
        
        return filled_df
    
    def clean_from_file(self, filepath: str, verbose: bool = True) -> pd.DataFrame:
        """
        從檔案讀取並清洗數據
        
        Args:
            filepath: 檔案路徑
            verbose: 是否顯示詳細資訊
            
        Returns:
            清洗後的數據
        """
        print(f"📂 讀取檔案: {filepath}")
        
        try:
            # 讀取CSV
            df = pd.read_csv(filepath)
            
            # 檢查必要欄位
            if 'timestamp' not in df.columns:
                raise ValueError("CSV必須包含 'timestamp' 欄位")
            
            # 轉換時間戳
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df = df.set_index('timestamp')
            
            # 執行清洗
            cleaned_df = self.clean(df, verbose)
            
            return cleaned_df
            
        except Exception as e:
            print(f"❌ 讀取或清洗失敗: {e}")
            raise


def test_data_cleaner():
    """測試數據清洗器"""
    print("🧪 測試數據清洗器...")
    
    # 創建測試數據
    dates = pd.date_range('2026-04-01', periods=100, freq='1min')
    close = 100 + np.cumsum(np.random.randn(100) * 0.5)
    
    test_df = pd.DataFrame({
        'Open': close - np.random.rand(100),
        'High': close + np.random.rand(100) * 2,
        'Low': close - np.random.rand(100) * 2,
        'Close': close,
        'Volume': 1000 + np.random.randn(100) * 200
    }, index=dates)
    
    # 添加一些日線數據模擬混合頻率
    daily_dates = pd.date_range('2026-04-01', periods=5, freq='1D')
    for date in daily_dates:
        test_df.loc[date] = {
            'Open': 105, 'High': 106, 'Low': 104, 'Close': 105.5, 'Volume': 5000
        }
    
    test_df = test_df.sort_index()
    
    # 創建清洗器
    cleaner = DataCleaner(target_freq='5min')
    
    # 檢測頻率
    freq_info = cleaner.detect_frequency(test_df)
    print(f"頻率檢測: {freq_info}")
    
    # 執行清洗
    cleaned_df = cleaner.clean(test_df)
    
    print(f"測試完成!")
    print(f"原始數據: {len(test_df)} 筆")
    print(f"清洗後數據: {len(cleaned_df)} 筆")
    
    return cleaned_df


if __name__ == "__main__":
    # 運行測試
    test_data_cleaner()
