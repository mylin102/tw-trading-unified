#!/usr/bin/env python3
"""
最終整合解決方案
整合：數據清洗 + DataEnricher + 技術指標計算
"""

import pandas as pd
import numpy as np
import sys
from pathlib import Path

# 添加項目根目錄到路徑
sys.path.insert(0, str(Path(__file__).parent.parent))

def create_complete_solution(ticker="2330"):
    """創建完整的解決方案"""
    print(f"🚀 創建完整解決方案 for {ticker}")
    print("=" * 60)
    
    # 步驟1: 數據清洗
    print(f"\n1️⃣ 步驟1: 數據清洗")
    from core.data_cleaner_fixed import DataCleanerFixed
    
    raw_path = f"data/taifex_raw/STOCK_{ticker}_5m.csv"
    
    # 讀取原始數據
    df_raw = pd.read_csv(raw_path)
    df_raw['timestamp'] = pd.to_datetime(df_raw['timestamp'])
    df_raw = df_raw.set_index('timestamp')
    
    print(f"  原始數據: {len(df_raw)} 筆")
    
    # 清洗數據
    cleaner = DataCleanerFixed(target_freq='5min')
    df_cleaned = cleaner.clean(df_raw, verbose=False)
    
    # 標準化欄位名稱 (大寫轉小寫)
    df_cleaned.columns = [col.lower() for col in df_cleaned.columns]
    
    print(f"  清洗後數據: {len(df_cleaned)} 筆")
    
    # 步驟2: 使用DataEnricher計算基本特徵
    print(f"\n2️⃣ 步驟2: 基本特徵計算")
    from core.data_enricher import DataEnricher
    
    enricher = DataEnricher()
    df_basic_features = enricher.enrich(df_cleaned, indicators=['atr', 'vwap', 'alpha'])
    
    print(f"  基本特徵計算完成")
    print(f"  欄位數: {len(df_basic_features.columns)}")
    
    # 步驟3: 計算技術指標
    print(f"\n3️⃣ 步驟3: 技術指標計算")
    from core.technical_indicators import TechnicalIndicators
    
    ti = TechnicalIndicators(
        adx_period=14,
        macd_fast=12,
        macd_slow=26,
        macd_signal=9,
        ma_short=20,
        ma_long=60,
        recent_lookback=20
    )
    
    df_complete = ti.calculate_all(df_basic_features)
    
    print(f"  技術指標計算完成")
    print(f"  總欄位數: {len(df_complete.columns)}")
    
    # 步驟4: 驗證KbarFeatureStrategy所需特徵
    print(f"\n4️⃣ 步驟4: 策略特徵驗證")
    
    required_columns = {
        "close", "high", "low", "atr", "vwap", "adx", "score",
        "regime", "bear_align", "bull_align", "bearish_align", "bullish_align",
        "macd_hist", "macd_rising", "mom_velo", "recent_high", "recent_low",
        "price_vs_vwap", "volume_spike",
    }
    
    existing_columns = set(df_complete.columns)
    missing = required_columns - existing_columns
    
    if missing:
        print(f"  ❌ 缺少欄位: {sorted(missing)}")
        
        # 嘗試修復缺失欄位
        df_complete = fix_missing_columns(df_complete, missing)
        
        # 重新檢查
        existing_columns = set(df_complete.columns)
        missing = required_columns - existing_columns
        
        if missing:
            print(f"  ❌ 修復後仍缺少: {sorted(missing)}")
        else:
            print(f"  ✅ 所有欄位已修復")
    else:
        print(f"  ✅ 所有必要欄位都存在")
    
    # 步驟5: 數據品質檢查
    print(f"\n5️⃣ 步驟5: 數據品質檢查")
    
    quality_report = check_data_quality(df_complete, required_columns)
    
    print(f"  數據品質報告:")
    print(f"    有效數據比例: {quality_report['valid_ratio']:.1%}")
    print(f"    問題數量: {quality_report['issue_count']}")
    
    if quality_report['issues']:
        print(f"    問題列表:")
        for issue in quality_report['issues'][:5]:  # 顯示前5個問題
            print(f"      - {issue}")
    
    # 步驟6: 保存最終數據
    print(f"\n6️⃣ 步驟6: 保存最終數據")
    
    output_dir = Path("data/final_solution")
    output_dir.mkdir(exist_ok=True)
    
    output_path = output_dir / f"STOCK_{ticker}_5m_complete.csv"
    df_complete.to_csv(output_path)
    
    print(f"  保存最終數據: {output_path}")
    print(f"  總筆數: {len(df_complete)}")
    print(f"  總欄位數: {len(df_complete.columns)}")
    
    # 步驟7: 測試策略可用性
    print(f"\n7️⃣ 步驟7: 策略可用性測試")
    
    strategy_test = test_strategy_readiness(df_complete)
    
    if strategy_test['ready']:
        print(f"  ✅ 策略準備就緒")
        print(f"    可用信號: {strategy_test['signal_count']}筆")
        print(f"    信號比例: {strategy_test['signal_ratio']:.1%}")
    else:
        print(f"  ⚠️ 策略可能無法正常運作")
        print(f"    問題: {strategy_test['issues']}")
    
    print(f"\n🎉 解決方案完成!")
    print(f"   股票: {ticker}")
    print(f"   數據: {len(df_complete)}筆 × {len(df_complete.columns)}欄位")
    print(f"   策略就緒: {'✅' if strategy_test['ready'] else '⚠️'}")
    
    return {
        'df_complete': df_complete,
        'output_path': output_path,
        'strategy_ready': strategy_test['ready'],
        'quality_report': quality_report
    }


def fix_missing_columns(df: pd.DataFrame, missing_columns: set) -> pd.DataFrame:
    """修復缺失欄位"""
    df_fixed = df.copy()
    
    for col in missing_columns:
        if col == 'price_vs_vwap' and 'vwap' in df_fixed.columns:
            # 計算價格vsVWAP
            df_fixed['price_vs_vwap'] = (df_fixed['close'] - df_fixed['vwap']) / df_fixed['vwap'] * 100
        
        elif col in ['close', 'high', 'low', 'open']:
            # 檢查是否有大寫版本
            upper_col = col.upper()
            if upper_col in df_fixed.columns:
                df_fixed[col] = df_fixed[upper_col]
        
        elif col == 'volume_spike' and 'volume' in df_fixed.columns:
            # 計算volume_spike
            vol_avg = df_fixed['volume'].rolling(20).mean()
            df_fixed['volume_spike'] = df_fixed['volume'] / vol_avg.replace(0, np.nan)
    
    return df_fixed


def check_data_quality(df: pd.DataFrame, required_columns: set) -> dict:
    """檢查數據品質"""
    report = {
        'total_rows': len(df),
        'valid_rows': 0,
        'valid_ratio': 0.0,
        'issues': [],
        'issue_count': 0
    }
    
    # 檢查每個必要欄位
    for col in required_columns:
        if col in df.columns:
            series = df[col]
            non_nan = series.notna().sum()
            nan_ratio = 1 - (non_nan / len(series))
            
            if nan_ratio > 0.5:  # 超過50%為NaN
                report['issues'].append(f"{col}: {nan_ratio:.1%}為NaN")
            
            # 檢查特殊值
            if col == 'volume_spike':
                ones = (series == 1).sum()
                if ones > len(series) * 0.8:  # 超過80%為1
                    report['issues'].append(f"{col}: {ones/len(series):.1%}為1")
            
            if col == 'trend_strength_raw':
                near_zero = ((series > -0.0001) & (series < 0.0001)).sum()
                if near_zero > len(series) * 0.8:  # 超過80%接近0
                    report['issues'].append(f"{col}: {near_zero/len(series):.1%}接近0")
        else:
            report['issues'].append(f"{col}: 欄位不存在")
    
    # 計算有效數據比例
    if required_columns:
        valid_counts = []
        for col in required_columns:
            if col in df.columns:
                valid_counts.append(df[col].notna().sum())
        
        if valid_counts:
            avg_valid = np.mean(valid_counts)
            report['valid_rows'] = int(avg_valid)
            report['valid_ratio'] = avg_valid / len(df)
    
    report['issue_count'] = len(report['issues'])
    
    return report


def test_strategy_readiness(df: pd.DataFrame) -> dict:
    """測試策略就緒狀態"""
    result = {
        'ready': False,
        'signal_count': 0,
        'signal_ratio': 0.0,
        'issues': []
    }
    
    # 檢查必要欄位
    required_for_signals = ['volume_spike', 'trend_strength_raw', 'adx', 'macd_hist']
    missing_for_signals = [col for col in required_for_signals if col not in df.columns]
    
    if missing_for_signals:
        result['issues'].append(f"缺少信號計算欄位: {missing_for_signals}")
        return result
    
    try:
        # 創建簡單的綜合信號
        signal_volume = (df['volume_spike'] > 1.2).astype(int)
        signal_trend = (df['trend_strength_raw'] > 0.001).astype(int)
        signal_adx = (df['adx'] > 25).astype(int)
        signal_macd = (df['macd_hist'] > 0).astype(int)
        
        # 綜合信號 (需要至少2個條件成立)
        combined_signal = ((signal_volume + signal_trend + signal_adx + signal_macd) >= 2).astype(int)
        
        result['signal_count'] = combined_signal.sum()
        result['signal_ratio'] = combined_signal.sum() / len(df)
        
        if result['signal_count'] > 0:
            result['ready'] = True
        else:
            result['issues'].append("策略未產生任何信號")
            
    except Exception as e:
        result['issues'].append(f"信號計算錯誤: {e}")
    
    return result


def run_backtest(ticker="2330"):
    """運行簡單回測"""
    print(f"\n📊 運行簡單回測 for {ticker}")
    
    # 加載完整數據
    data_path = f"data/final_solution/STOCK_{ticker}_5m_complete.csv"
    
    if not Path(data_path).exists():
        print(f"❌ 找不到數據: {data_path}")
        return
    
    df = pd.read_csv(data_path, index_col='timestamp', parse_dates=True)
    
    print(f"  回測數據: {len(df)}筆")
    
    # 簡單的移動平均交叉策略
    df['ma_fast'] = df['close'].rolling(10).mean()
    df['ma_slow'] = df['close'].rolling(30).mean()
    
    # 信號
    df['signal'] = 0
    df.loc[df['ma_fast'] > df['ma_slow'], 'signal'] = 1  # 買入
    df.loc[df['ma_fast'] < df['ma_slow'], 'signal'] = -1  # 賣出
    
    # 計算回報
    df['returns'] = df['close'].pct_change()
    df['strategy_returns'] = df['signal'].shift(1) * df['returns']
    
    # 移除NaN
    df_clean = df.dropna()
    
    if len(df_clean) == 0:
        print(f"  ⚠️ 無有效回測數據")
        return
    
    # 計算績效指標
    total_return = (1 + df_clean['strategy_returns']).prod() - 1
    sharpe_ratio = df_clean['strategy_returns'].mean() / df_clean['strategy_returns'].std() * np.sqrt(252 * 12)  # 年化
    
    print(f"  回測結果:")
    print(f"    總報酬率: {total_return:.2%}")
    print(f"    夏普比率: {sharpe_ratio:.2f}")
    print(f"    交易次數: {(df_clean['signal'].diff() != 0).sum()}")
    
    return {
        'total_return': total_return,
        'sharpe_ratio': sharpe_ratio,
        'trade_count': (df_clean['signal'].diff() != 0).sum()
    }


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="最終整合解決方案")
    parser.add_argument("--ticker", default="2330", help="股票代號")
    parser.add_argument("--backtest", action="store_true", help="運行回測")
    args = parser.parse_args()
    
    if args.backtest:
        run_backtest(args.ticker)
    else:
        create_complete_solution(args.ticker)
