#!/usr/bin/env python3
"""簡單測試多時間框架分析整合"""

import pandas as pd
import numpy as np
from datetime import datetime

def test_multi_timeframe_simple():
    """簡單測試多時間框架分析整合"""
    print("=== 簡單測試多時間框架分析整合 ===")
    
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
    
    # 測試多時間框架分析
    from strategies.stocks.multi_timeframe import analyze_market_condition, should_trade_based_on_tf
    
    print("1. 測試多時間框架分析...")
    analysis = analyze_market_condition(df)
    print(f"   市場狀態: {analysis.get('market_state', {})}")
    print(f"   交易建議: {analysis.get('trading_recommendation', {})}")
    
    print("2. 測試交易決策...")
    should_trade, details = should_trade_based_on_tf(df)
    print(f"   是否應該交易: {should_trade}")
    
    print("3. 測試基本均值回歸策略...")
    # 模擬state結構
    state = {
        "df_5m": df,
        "last_5m": df.iloc[-1].copy()
    }
    
    # 確保價格低於布林下軌
    state["last_5m"]["Close"] = df['bb_lower'].iloc[-1] * 0.99
    
    # 測試基本策略
    from strategies.stocks.entry_strategies import strategy_stock_mean_reversion
    
    cfg = {
        "stop_loss_pct": 0.05,
        "take_profit_pct": 0.15
    }
    
    result = strategy_stock_mean_reversion(state, cfg)
    
    if result:
        print(f"   基本策略結果: {result.get('action')} - {result.get('reason')}")
    else:
        print("   基本策略未觸發信號")
    
    print("\n=== 測試完成 ===")
    return True

if __name__ == "__main__":
    try:
        test_multi_timeframe_simple()
        print("✅ 簡單測試通過")
    except Exception as e:
        print(f"❌ 測試失敗: {e}")
        import traceback
        traceback.print_exc()