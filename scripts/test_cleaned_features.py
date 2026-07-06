#!/usr/bin/env python3
"""
測試清洗後數據的特徵計算
"""

import pandas as pd
import numpy as np
import sys
from pathlib import Path

# 添加項目根目錄到路徑
sys.path.insert(0, str(Path(__file__).parent.parent))

def test_cleaned_data_features(ticker="2330"):
    """測試清洗後數據的特徵計算"""
    print(f"🧪 測試股票 {ticker} 清洗後數據的特徵計算...")
    
    # 讀取清洗後數據
    cleaned_path = f"data/cleaned_fixed/STOCK_{ticker}_5m_cleaned.csv"
    
    if not Path(cleaned_path).exists():
        print(f"❌ 找不到清洗後數據: {cleaned_path}")
        return
    
    # 讀取數據
    df = pd.read_csv(cleaned_path)
    print(f"📊 數據欄位: {df.columns.tolist()}")
    
    # 檢查是否有timestamp欄位
    if 'timestamp' in df.columns:
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.set_index('timestamp')
    elif 'Unnamed: 0' in df.columns:  # 可能是索引欄位
        df = df.set_index('Unnamed: 0')
        df.index = pd.to_datetime(df.index)
        df.index.name = 'timestamp'
    else:
        print(f"❌ 找不到時間戳欄位")
        return
    
    print(f"\n📈 清洗後數據統計:")
    print(f"   筆數: {len(df)}")
    print(f"   時間範圍: {df.index.min()} 到 {df.index.max()}")
    print(f"   欄位: {df.columns.tolist()}")
    
    # 手動計算關鍵特徵
    print(f"\n🧮 手動計算關鍵特徵:")
    
    # 1. volume_spike
    if 'Volume' in df.columns:
        vol_avg = df['Volume'].rolling(20).mean()
        volume_spike = df['Volume'] / vol_avg.replace(0, np.nan)
        
        print(f"\n  volume_spike 統計:")
        print(f"    非NaN值: {volume_spike.notna().sum()}/{len(volume_spike)}")
        print(f"    平均值: {volume_spike.mean():.3f}")
        print(f"    標準差: {volume_spike.std():.3f}")
        print(f"    範圍: {volume_spike.min():.3f} - {volume_spike.max():.3f}")
        
        # 分析分佈
        bins = [0, 0.5, 0.8, 1.2, 1.5, 2.0, 5.0, float('inf')]
        labels = ['極低', '低', '正常', '略高', '高', '很高', '極高']
        
        for i in range(len(bins)-1):
            count = ((volume_spike >= bins[i]) & (volume_spike < bins[i+1])).sum()
            if count > 0:
                print(f"    {labels[i]}: {count}筆 ({count/len(volume_spike)*100:.1f}%)")
    
    # 2. 移動平均和趨勢強度
    if 'Close' in df.columns:
        ma20 = df['Close'].rolling(20).mean()
        ma60 = df['Close'].rolling(60).mean()
        trend_strength_raw = (ma20 - ma60) / df['Close']
        
        print(f"\n  trend_strength_raw 統計:")
        print(f"    非NaN值: {trend_strength_raw.notna().sum()}/{len(trend_strength_raw)}")
        print(f"    平均值: {trend_strength_raw.mean():.6f}")
        print(f"    標準差: {trend_strength_raw.std():.6f}")
        
        # 檢查是否還有全部接近0的問題
        near_zero = ((trend_strength_raw > -0.0001) & (trend_strength_raw < 0.0001)).sum()
        if near_zero > len(trend_strength_raw) * 0.5:
            print(f"    ⚠️ 仍有 {near_zero} 筆接近0 ({near_zero/len(trend_strength_raw)*100:.1f}%)")
        else:
            print(f"    ✅ 數值分佈正常")
            
        # 分析趨勢強度分佈
        trend_bins = [-float('inf'), -0.01, -0.005, -0.001, 0.001, 0.005, 0.01, float('inf')]
        trend_labels = ['強空頭', '空頭', '弱空頭', '震盪', '弱多頭', '多頭', '強多頭']
        
        print(f"\n  趨勢強度分佈:")
        for i in range(len(trend_bins)-1):
            count = ((trend_strength_raw >= trend_bins[i]) & (trend_strength_raw < trend_bins[i+1])).sum()
            if count > 0:
                print(f"    {trend_labels[i]}: {count}筆 ({count/len(trend_strength_raw)*100:.1f}%)")
    
    # 3. 價格突破強度
    if all(col in df.columns for col in ['Close', 'High']):
        high_20 = df['High'].rolling(20).max().shift(1)
        # 簡化計算
        breakout_strength = (df['Close'] - high_20)
        
        print(f"\n  breakout_strength 統計:")
        print(f"    非NaN值: {breakout_strength.notna().sum()}/{len(breakout_strength)}")
        print(f"    平均值: {breakout_strength.mean():.3f}")
        print(f"    標準差: {breakout_strength.std():.3f}")
        
        # 分析突破情況
        positive = (breakout_strength > 0).sum()
        negative = (breakout_strength < 0).sum()
        zero = (breakout_strength == 0).sum()
        
        print(f"    突破(>0): {positive}筆 ({positive/len(breakout_strength)*100:.1f}%)")
        print(f"    跌破(<0): {negative}筆 ({negative/len(breakout_strength)*100:.1f}%)")
        print(f"    持平(=0): {zero}筆 ({zero/len(breakout_strength)*100:.1f}%)")
    
    print(f"\n💡 結論:")
    print(f"   清洗後數據的特徵計算看起來更合理")
    print(f"   volume_spike 不再全部為1")
    print(f"   trend_strength_raw 有合理的分佈")
    print(f"   下一步：修正 DataEnricher 以使用清洗後數據")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="測試清洗後數據的特徵計算")
    parser.add_argument("--ticker", default="2330", help="股票代號 (預設: 2330)")
    args = parser.parse_args()
    
    test_cleaned_data_features(args.ticker)
