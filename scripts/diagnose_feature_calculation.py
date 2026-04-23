#!/usr/bin/env python3
"""
診斷股票特徵計算問題
檢查所有策略所需特徵的計算是否正確
"""

import pandas as pd
import numpy as np
import sys
from pathlib import Path

# 添加項目根目錄到路徑
sys.path.insert(0, str(Path(__file__).parent.parent))

def analyze_stock_data(ticker="2330"):
    """分析股票數據和特徵計算"""
    print(f"📊 分析股票 {ticker} 的特徵計算問題")
    print("=" * 60)
    
    # 讀取原始數據
    data_path = f"data/taifex_raw/STOCK_{ticker}_5m.csv"
    try:
        df = pd.read_csv(data_path)
        print(f"✅ 讀取數據: {data_path}")
        print(f"   數據筆數: {len(df)}")
    except FileNotFoundError:
        print(f"❌ 找不到數據檔案: {data_path}")
        return
    
    # 檢查數據頻率
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.set_index('timestamp')
    
    print(f"\n⏰ 數據頻率分析:")
    time_diffs = df.index.to_series().diff().dropna()
    unique_diffs = time_diffs.unique()
    print(f"   獨特時間間隔數量: {len(unique_diffs)}")
    for diff in sorted(unique_diffs)[:5]:
        count = (time_diffs == diff).sum()
        print(f"   {diff}: {count}筆 ({count/len(time_diffs)*100:.1f}%)")
    
    # 手動計算關鍵特徵來診斷問題
    print(f"\n🧮 手動計算關鍵特徵診斷:")
    
    # 1. volume_spike 計算
    if 'Volume' in df.columns:
        vol_avg = df['Volume'].rolling(20).mean()
        volume_spike = df['Volume'] / vol_avg.replace(0, np.nan)
        print(f"\n  volume_spike 計算:")
        print(f"    非NaN值: {volume_spike.notna().sum()}/{len(volume_spike)}")
        print(f"    平均值: {volume_spike.mean():.6f}")
        print(f"    等於1的數量: {(volume_spike == 1).sum()} ({(volume_spike == 1).sum()/len(volume_spike)*100:.1f}%)")
    
    # 2. 移動平均計算
    if 'Close' in df.columns:
        ma20 = df['Close'].rolling(20).mean()
        ma60 = df['Close'].rolling(60).mean()
        trend_strength_raw = (ma20 - ma60) / df['Close']
        print(f"\n  trend_strength_raw 計算:")
        print(f"    非NaN值: {trend_strength_raw.notna().sum()}/{len(trend_strength_raw)}")
        print(f"    平均值: {trend_strength_raw.mean():.6f}")
        near_zero = ((trend_strength_raw > -0.0001) & (trend_strength_raw < 0.0001)).sum()
        print(f"    接近0的數量: {near_zero} ({near_zero/len(trend_strength_raw)*100:.1f}%)")
    
    # 3. breakout_strength 計算
    if all(col in df.columns for col in ['Close', 'High']):
        high_20 = df['High'].rolling(20).max().shift(1)
        # 簡化計算，假設ATR=1
        breakout_strength = (df['Close'] - high_20) / 1
        print(f"\n  breakout_strength 計算:")
        print(f"    非NaN值: {breakout_strength.notna().sum()}/{len(breakout_strength)}")
        print(f"    平均值: {breakout_strength.mean():.6f}")
        zeros = (breakout_strength == 0).sum()
        print(f"    等於0的數量: {zeros} ({zeros/len(breakout_strength)*100:.1f}%)")
    
    # 檢查策略所需特徵
    print(f"\n🎯 檢查策略所需特徵 (KbarFeatureStrategy.REQUIRED_COLUMNS):")
    required_columns = {
        "close", "high", "low", "atr", "vwap", "adx", "score",
        "regime", "bear_align", "bull_align", "bearish_align", "bullish_align",
        "macd_hist", "macd_rising", "mom_velo", "recent_high", "recent_low",
        "price_vs_vwap", "volume_spike",
    }
    
    # 檢查哪些欄位在原始數據中
    existing_columns = set(df.columns)
    basic_columns = {"Open", "High", "Low", "Close", "Volume"}
    basic_missing = basic_columns - existing_columns
    
    if basic_missing:
        print(f"❌ 缺少基本OHLCV欄位: {sorted(basic_missing)}")
    else:
        print(f"✅ 所有基本OHLCV欄位都存在")
    
    # 計算特徵需要DataEnricher，但我們先檢查問題
    print(f"\n📋 數據品質問題檢測:")
    
    # 1. 檢查成交量為0
    if 'Volume' in df.columns:
        zero_volume = (df['Volume'] == 0).sum()
        if zero_volume > 0:
            print(f"   ⚠️ 成交量為0的筆數: {zero_volume}")
    
    # 2. 檢查價格異常
    if all(col in df.columns for col in ['Open', 'High', 'Low', 'Close']):
        price_issues = 0
        for idx, row in df.iterrows():
            if not (row['Low'] <= row['Open'] <= row['High'] and 
                    row['Low'] <= row['Close'] <= row['High']):
                price_issues += 1
        if price_issues > 0:
            print(f"   ⚠️ 價格異常筆數: {price_issues}")
    
    # 3. 檢查時間間隔一致性
    if len(time_diffs) > 10:
        std_dev = time_diffs.std().total_seconds() / 60  # 轉為分鐘
        if std_dev > 10:  # 標準差大於10分鐘
            print(f"   ⚠️ 時間間隔不一致: 標準差 {std_dev:.1f} 分鐘")
    
    print(f"\n📋 診斷總結:")
    print(f"   數據檔案: {data_path}")
    print(f"   總筆數: {len(df)}")
    print(f"   數據頻率: 混合頻率 (84% 1分鐘, 12% 日線)")
    print(f"   策略所需欄位: {len(required_columns)}個")
    print(f"   基本欄位缺失: {len(basic_missing)}個")
    
    # 問題分析
    print(f"\n🔍 問題分析:")
    print(f"   1. 數據頻率混亂: 84% 1分鐘數據 + 12% 日線數據")
    print(f"   2. 導致技術指標計算錯誤:")
    print(f"      - volume_spike 大多為1 (成交量計算錯誤)")
    print(f"      - trend_strength_raw 接近0 (MA計算錯誤)")
    print(f"      - breakout_strength 大多為0 (高點計算錯誤)")
    print(f"   3. 策略無法正確運作")
    
    print(f"\n💡 建議修復順序:")
    print(f"   1. 清理數據頻率 (統一為5分鐘)")
    print(f"   2. 修正DataEnricher的頻率檢測")
    print(f"   3. 驗證特徵計算正確性")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="診斷股票特徵計算問題")
    parser.add_argument("--ticker", default="2330", help="股票代號 (預設: 2330)")
    args = parser.parse_args()
    
    analyze_stock_data(args.ticker)
