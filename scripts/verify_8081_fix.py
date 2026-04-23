
import pandas as pd
import numpy as np
from strategies.stocks.multi_timeframe import MultiTimeframeAnalyzer
from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze

# 1. 讀取昨天的 8081 數據
df = pd.read_csv('logs/market_data/STOCK_8081_20260422_indicators.csv')
# 為了模擬計算，我們只需要基礎價格列
df_raw = df[['open', 'high', 'low', 'close', 'volume']].copy()
df_raw.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
df_raw.index = pd.to_datetime(range(len(df_raw)), unit='m') # 模擬時間戳

# 2. 執行新的 5m Resample + Multi-TF 分析
analyzer = MultiTimeframeAnalyzer()
# 先模擬 5m 採樣
df_5m = df_raw.iloc[::5].copy() 
analysis = analyzer.analyze_multi_timeframe(df_5m)

print(f"--- 8081 趨勢判定驗證 (基於昨日數據) ---")
print(f"舊邏輯 (EMA 交叉): 🔴 空頭 (EMA20 < EMA60)")
print(f"新邏輯 (Multi-TF): {'🟢 多頭' if analysis['market_state']['primary_trend'] == 'BULL' else '⚪ 中性/🔴 空頭'}")
print(f"60m 趨勢狀態: {analysis['timeframe_analysis'].get('60m', {}).get('trend')}")
print(f"60m 價格變動: {analysis['timeframe_analysis'].get('60m', {}).get('price_change_pct', 0):.2f}%")
