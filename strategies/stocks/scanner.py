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
                
                last = df.iloc[-1]
                # 判定為 SQUEEZING 或 FIRED 狀態
                if last["sqz_on"] or last["fired"]:
                    results.append({
                        "ticker": ticker,
                        "status": "SQUEEZING" if last["sqz_on"] else "FIRED",
                        "close": last["Close"],
                        "score": last.get("score", 0),
                        "adx": last.get("adx", 0)
                    })
            except Exception as e:
                print(f"⚠️ Failed to scan {ticker}: {e}")
        return pd.DataFrame(results)
