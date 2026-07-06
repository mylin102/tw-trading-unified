#!/usr/bin/env python3
"""調試均值回歸策略"""

import pandas as pd
import numpy as np
from datetime import datetime

def test_debug_mean_reversion():
    """調試均值回歸策略"""
    print("=== 調試均值回歸策略 ===")
    
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
    
    # 添加布林帶
    df['bb_lower'] = df['Close'].rolling(20).mean() - 2 * df['Close'].rolling(20).std()
    
    print(f"DataFrame columns: {df.columns.tolist()}")
    print(f"Last row has bb_lower: {'bb_lower' in df.iloc[-1]}")
    print(f"Last Close: {df.iloc[-1]['Close']}")
    print(f"Last bb_lower: {df.iloc[-1]['bb_lower']}")
    
    # 修改最後一個價格使其低於布林下軌
    df.loc[df.index[-1], 'Close'] = df['bb_lower'].iloc[-1] * 0.99
    
    print(f"\nAfter modification:")
    print(f"Last Close: {df.iloc[-1]['Close']}")
    print(f"Last bb_lower: {df.iloc[-1]['bb_lower']}")
    print(f"Close < bb_lower: {df.iloc[-1]['Close'] < df.iloc[-1]['bb_lower']}")
    
    # 測試基本策略
    from strategies.stocks.entry_strategies import strategy_stock_mean_reversion
    
    state = {
        "df_5m": df,
        "last_5m": df.iloc[-1]
    }
    
    cfg = {
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.15
    }
    
    result = strategy_stock_mean_reversion(state, cfg)
    
    print(f"\n策略結果: {result}")
    
    print("\n=== 調試完成 ===")

if __name__ == "__main__":
    try:
        test_debug_mean_reversion()
        print("✅ 調試完成")
    except Exception as e:
        print(f"❌ 調試失敗: {e}")
        import traceback
        traceback.print_exc()