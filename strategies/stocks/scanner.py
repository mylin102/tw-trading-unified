import pandas as pd
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze

class StockScanner:
    def __init__(self, api):
        self.api = api

    def scan_squeeze(self, tickers: list):
        """
        使用【整股數據】進行策略掃描 (分析用整股，執行用零股)。
        整股數據交易量大，指標參考性較高。
        """
        results = []
        for ticker in tickers:
            try:
                # 1. 取得合約 (預設為整股)
                contract = self.api.Contracts.Stocks[ticker]
                
                # 2. 抓取整股歷史 K 線 (不傳 odd_lot 參數)
                # 抓取最近 5 個交易日的 5分K
                start_date = (pd.Timestamp.now() - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
                kbars = self.api.kbars(contract, start=start_date)
                
                df = pd.DataFrame({**kbars})
                if df.empty:
                    continue
                
                df.ts = pd.to_datetime(df.ts)
                df = df.set_index("ts")
                
                # 3. 執行指標運算 (Squeeze, ADX, etc.)
                df = calculate_futures_squeeze(df)
                
                # --- 新增：投信作帳策略指標 (CL3 Spec) ---
                # MA20/MA60 (5分鐘K線模擬日線：1日約54根5分K，此處使用約略值)
                df['ma20'] = df['Close'].rolling(20 * 12).mean()
                df['ma60'] = df['Close'].rolling(60 * 12).mean()
                
                # 投信連三買代理指標 (IT Proxy): 
                # 邏輯：成交量 > 均量 1.2倍 且 收紅 且 價格 > 均線，視為機構建倉。
                vol_avg = df['Volume'].rolling(20).mean()
                is_it_buy = (df['Volume'] > vol_avg * 1.2) & (df['Close'] > df['Open']) & (df['Close'] > df['ma20'])
                # 模擬連三買 (滾動加總，若最近三天都有買點則 > 0)
                df['it_buy_rolling_3_min'] = is_it_buy.rolling(3).min().astype(int)
                
                last = df.iloc[-1]
                # 判定為 SQUEEZING 或 FIRED 狀態
                results.append({
                    "ticker": ticker,
                    "status": "SQUEEZING" if last["sqz_on"] else "FIRED",
                    "close": last["Close"],
                    "score": last.get("score", 0),
                    "adx": last.get("adx", 0),
                    "it_buy": "🔥 強力建倉" if last["it_buy_rolling_3_min"] > 0 else "⚪ 中性"
                })
            except Exception as e:
                print(f"⚠️ Failed to scan {ticker}: {e}")
        return pd.DataFrame(results)
