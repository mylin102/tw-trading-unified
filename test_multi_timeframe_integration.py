#!/usr/bin/env python3
"""測試多時間框架分析整合"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta

def test_multi_timeframe_integration():
    """測試多時間框架分析整合"""
    print("=== 測試多時間框架分析整合 ===")
    
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
    
    # 測試多時間框架分析
    from strategies.stocks.multi_timeframe import analyze_market_condition, should_trade_based_on_tf
    
    print("1. 測試多時間框架分析...")
    analysis = analyze_market_condition(df)
    print(f"   市場狀態: {analysis.get('market_state', {})}")
    print(f"   交易建議: {analysis.get('trading_recommendation', {})}")
    
    print("2. 測試交易決策...")
    should_trade, details = should_trade_based_on_tf(df)
    print(f"   是否應該交易: {should_trade}")
    print(f"   詳細信息: {details.get('trading_recommendation', {})}")
    
    print("3. 測試均值回歸策略整合...")
    # 模擬state結構
    state = {
        "last_5m": df.iloc[-1],
        "df_5m": df,
        "scout_stage": "IDLE",
        "scout_entry_price": 0.0,
        "market_trend": "BULL",
        "is_bear_market": False,
        "pattern": "NONE",
        "pivot": 0.0,
        "multi_timeframe": analysis,
        "market_state": analysis.get('market_state', {}),
        "tf_recommendation": analysis.get('trading_recommendation', {}),
        "should_trade_tf": should_trade,
        "tf_details": details
    }
    
    # 測試配置
    cfg = {
        "stocks": {
            "stop_loss_pct": 0.05,
            "take_profit_pct": 0.15,
            "trailing_stop_pct": 0.02,
            "atr_stop_multiplier": 2.0
        }
    }
    
    # 測試策略函數
    from strategies.stocks.entry_strategies import strategy_stock_mean_reversion_enhanced
    
    # 添加布林帶數據以觸發策略
    df['bb_lower'] = df['Close'].rolling(20).mean() - 2 * df['Close'].rolling(20).std()
    # 確保最後一個價格低於布林下軌
    df.loc[df.index[-1], 'Close'] = df['bb_lower'].iloc[-1] * 0.99
    # 更新state中的last_5m
    state["last_5m"] = df.iloc[-1].copy()
    state["last_5m"]["rsi"] = 25  # 超賣
    state["last_5m"]["k_val"] = 15  # KD超賣
    state["last_5m"]["atr"] = 0.5
    
    result = strategy_stock_mean_reversion_enhanced(state, cfg)
    
    if result:
        print(f"   策略結果: {result.get('action')} - {result.get('reason')}")
        print(f"   包含多時間框架信息: {'multi_timeframe_used' in result.get('metadata', {})}")
    else:
        print("   策略未觸發信號")
    
    print("\n=== 測試完成 ===")
    return True

if __name__ == "__main__":
    try:
        test_multi_timeframe_integration()
        print("✅ 多時間框架分析整合測試通過")
    except Exception as e:
        print(f"❌ 測試失敗: {e}")
        import traceback
        traceback.print_exc()
