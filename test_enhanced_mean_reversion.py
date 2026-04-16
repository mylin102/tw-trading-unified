#!/usr/bin/env python3
"""測試增強版均值回歸策略"""

import pandas as pd
import numpy as np
from datetime import datetime

def test_enhanced_mean_reversion():
    """測試增強版均值回歸策略"""
    print("=== 測試增強版均值回歸策略 ===")
    
    # 創建測試數據
    dates = pd.date_range(start='2026-04-12 09:00', periods=100, freq='5min')
    np.random.seed(42)
    base_price = 100
    prices = base_price + np.cumsum(np.random.randn(100) * 0.3)
    
    df = pd.DataFrame({
        'Open': prices - np.random.rand(100) * 0.2,
        'High': prices + np.random.rand(100) * 0.3,
        'Low': prices - np.random.rand(100) * 0.3,
        'Close': prices,
        'Volume': np.random.randint(1000, 5000, 100)
    }, index=dates)
    
    # 添加布林帶和ATR
    df['bb_lower'] = df['Close'].rolling(20).mean() - 2 * df['Close'].rolling(20).std()
    
    # 計算ATR
    high_low = df['High'] - df['Low']
    high_close = np.abs(df['High'] - df['Close'].shift())
    low_close = np.abs(df['Low'] - df['Close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(14).mean()
    
    # 修改最後一個價格使其低於布林下軌
    df.loc[df.index[-1], 'Close'] = df['bb_lower'].iloc[-1] * 0.99
    
    print(f"測試條件:")
    print(f"  Close: {df.iloc[-1]['Close']}")
    print(f"  bb_lower: {df.iloc[-1]['bb_lower']}")
    print(f"  Close < bb_lower: {df.iloc[-1]['Close'] < df.iloc[-1]['bb_lower']}")
    print(f"  ATR: {df.iloc[-1]['atr']}")
    
    # 測試增強策略
    from strategies.stocks.entry_strategies import strategy_stock_mean_reversion_enhanced
    
    state = {
        "df_5m": df,
        "last_5m": df.iloc[-1]
    }
    
    cfg = {
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.15,
        "use_atr_stop_loss": True,
        "atr_stop_multiplier": 2.0
    }
    
    result = strategy_stock_mean_reversion_enhanced(state, cfg)
    
    print(f"\n增強策略結果:")
    if result:
        print(f"  Action: {result.get('action')}")
        print(f"  Reason: {result.get('reason')}")
        print(f"  Stop Loss: {result.get('stop_loss')}")
        metadata = result.get('metadata', {})
        print(f"  Metadata:")
        print(f"    multi_timeframe_used: {metadata.get('multi_timeframe_used', False)}")
        print(f"    market_regime: {metadata.get('market_regime', 'UNKNOWN')}")
        print(f"    primary_trend: {metadata.get('primary_trend', 'UNKNOWN')}")
        print(f"    filters_passed: {metadata.get('filters_passed', 0)}/{metadata.get('total_filters', 4)}")
    else:
        print("  策略未觸發信號")
    
    print("\n=== 測試完成 ===")

if __name__ == "__main__":
    try:
        test_enhanced_mean_reversion()
        print("✅ 增強版均值回歸策略測試完成")
    except Exception as e:
        print(f"❌ 測試失敗: {e}")
        import traceback
        traceback.print_exc()