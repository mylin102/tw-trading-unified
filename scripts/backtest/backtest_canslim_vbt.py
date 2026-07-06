"""
CANSLIM Vectorized Backtest (10-Year Stress Test)
Integrates pattern_engine.py with vectorbt for portfolio-level analysis.
"""
import os
import sys
import pandas as pd
import numpy as np
import vectorbt as vbt
import yfinance as yf
from pathlib import Path
from datetime import datetime, timedelta

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks.pattern_engine import detect_cup_with_handle

# ====================== 參數設定 ======================
# 台灣前 20 大權值股 (可擴充至 0050)
TICKERS = [
    '2330.TW', '2454.TW', '2317.TW', '2308.TW', '2303.TW', 
    '2881.TW', '2882.TW', '3711.TW', '2412.TW', '2886.TW',
    '2382.TW', '2891.TW', '1301.TW', '1303.TW', '2884.TW',
    '1216.TW', '2892.TW', '2002.TW', '2357.TW', '3008.TW'
]
INDEX_TICKER = '^TWII' # 加權指數
START_DATE = '2015-01-01'
END_DATE = datetime.now().strftime('%Y-%m-%d')

class VBT_CANSLIM_Engine:
    def __init__(self, tickers, index_ticker):
        self.tickers = tickers
        self.index_ticker = index_ticker
        self.data = None
        self.index_data = None

    def download_data(self):
        print(f"📥 Downloading data for {len(self.tickers)} tickers...")
        # 下載個股
        self.data = vbt.YFData.download(self.tickers, start=START_DATE, end=END_DATE)
        # 下載大盤
        self.index_data = vbt.YFData.download(self.index_ticker, start=START_DATE, end=END_DATE)
        return self.data.get('Close').dropna(how='all', axis=1)

    def generate_signals(self, close, volume):
        """
        核心邏輯：將 pattern_engine 的幾何偵測轉為 VBT 訊號矩陣
        """
        entries = pd.DataFrame(False, index=close.index, columns=close.columns)
        pivots = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
        
        # 1. 市場趨勢 (M): 大盤 > 200MA (或是更敏感的 EMA60)
        index_close = self.index_data.get('Close')
        market_ma = index_close.rolling(200).mean()
        market_up = index_close > market_ma

        print("🔍 Scanning for Geometric Patterns (this may take a minute)...")
        for ticker in close.columns:
            # 取得該股的完整歷史
            ticker_df = pd.DataFrame({
                'Close': close[ticker],
                'High': self.data.get('High')[ticker],
                'Low': self.data.get('Low')[ticker],
                'Volume': volume[ticker]
            }).dropna()
            
            if len(ticker_df) < 100: continue

            # 每個月掃描一次型態 (降低運算量，模擬真實掃描頻率)
            scan_dates = ticker_df.index[::20] 
            
            for d in scan_dates:
                # 只有大盤多頭才掃描
                if not market_up.loc[d]: continue
                
                # 截取到掃描日為止的歷史
                hist_to_date = ticker_df.loc[:d]
                res = detect_cup_with_handle(hist_to_date)
                
                if res["status"]:
                    pivot = res["pivot_price"]
                    # 尋找「掃描日之後」的突破點 (Window: 未來 20 天)
                    future_df = ticker_df.loc[d:].head(20)
                    
                    # 突破條件：價格 > Pivot 且 成交量爆發
                    vol_avg = ticker_df['Volume'].rolling(20).mean().loc[d]
                    
                    breakout_mask = (future_df['Close'] > pivot) & (future_df['Volume'] > vol_avg * 1.4)
                    
                    if breakout_mask.any():
                        breakout_date = breakout_mask.idxmax()
                        entries.loc[breakout_date, ticker] = True
                        pivots.loc[breakout_date, ticker] = pivot

        return entries, pivots

    def run(self):
        close = self.download_data()
        volume = self.data.get('Volume')
        
        entries, pivots = self.generate_signals(close, volume)
        
        print("🚀 Running Portfolio Simulation...")
        # 執行回測
        # fees=0.004 (包含證交稅 0.3% + 手續費)
        pf = vbt.Portfolio.from_signals(
            close=close,
            entries=entries,
            exits=pd.DataFrame(False, index=close.index, columns=close.columns), # 靠 SL/TS 出場
            init_cash=1000000,
            fees=0.004,
            sl_stop=0.07,           # 7% 硬性止損 (O'Neil 建議 7-8%)
            tp_stop=0.20,           # 20% 初步止盈 (CANSLIM 建議在 20-25% 先鎖定部分利潤)
            ts_stop=0.10,           # 10% 移動止損 (利潤奔跑)
            cash_sharing=True,      # 資金共用池
            size=0.1,               # 每筆交易最多佔總資金 10% (分散風險)
            size_type='value_pct'
        )
        
        print("\n" + "="*30)
        print("📊 CANSLIM 10-YEAR STRESS TEST")
        print("="*30)
        print(pf.stats())
        
        # 繪製淨值曲線
        try:
            fig = pf.plot()
            fig.show()
        except:
            print("Plotting skipped (no display available).")
            
        # 儲存結果
        output_path = ROOT / "exports" / "backtest_canslim_vbt_results.csv"
        pf.trades.to_pd().to_csv(output_path)
        print(f"\n✅ Trades saved to {output_path}")

if __name__ == "__main__":
    # 提醒使用者安裝必要庫
    # pip install vectorbt yfinance scipy
    engine = VBT_CANSLIM_Engine(TICKERS, INDEX_TICKER)
    engine.run()
