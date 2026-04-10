import pandas as pd
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
from strategies.stocks.pattern_engine import detect_cup_with_handle

class StockScanner:
    def __init__(self, api):
        self.api = api

    def scan_squeeze(self, tickers: list, config: dict = None):
        """
        使用【整股數據】進行策略掃描 (分析用整股，執行用零股)。
        整股數據交易量大，指標參考性較高。
        """
        results = []
        canslim_cfg = (config or {}).get("stocks", {}).get("canslim", {})
        
        for ticker in tickers:
            try:
                # 1. 取得合約 (預設為整股)
                contract = self.api.Contracts.Stocks[ticker]
                
                # 2. 抓取整股 1分K 數據 (最近 7 天，用於合成 5分K)
                start_date_5m = (pd.Timestamp.now() - pd.Timedelta(days=7)).strftime("%Y-%m-%d")
                kbars_1m = self.api.kbars(contract, start=start_date_5m)
                df_1m = pd.DataFrame({**kbars_1m})
                if df_1m.empty: continue
                df_1m.ts = pd.to_datetime(df_1m.ts)
                df_1m = df_1m.set_index("ts")
                
                # 合成 5分K
                df_5m = df_1m.resample("5min").agg({
                    "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
                }).dropna()
                df_5m = calculate_futures_squeeze(df_5m)
                
                # 3. 抓取整股 1分K 數據 (最近 1 年，用於合成日線)
                # 💡 GSD: Fetching 1 year of 1min data is VERY slow. 
                # Shioaji kbars might return limited rows. 
                # Better to fetch from a dedicated history source if available.
                start_date_1d = (pd.Timestamp.now() - pd.Timedelta(days=365)).strftime("%Y-%m-%d")
                kbars_raw = self.api.kbars(contract, start=start_date_1d)
                df_raw = pd.DataFrame({**kbars_raw})
                
                pattern_status = "NONE"
                pivot_price = 0.0
                
                if not df_raw.empty:
                    df_raw.ts = pd.to_datetime(df_raw.ts)
                    df_raw = df_raw.set_index("ts")
                    
                    # 合成日線 (CANSLIM 必備)
                    df_1d = df_raw.resample("1D").agg({
                        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
                    }).dropna()
                    df_1d.columns = [c.capitalize() for c in df_1d.columns]
                    
                    # 執行幾何型態偵測
                    base_info = detect_cup_with_handle(df_1d, 
                        cup_depth_min=canslim_cfg.get("cup_depth_min", 0.12),
                        cup_depth_max=canslim_cfg.get("cup_depth_max", 0.35)
                    )
                    if base_info["status"]:
                        pattern_status = base_info["type"].upper()
                        pivot_price = base_info["pivot_price"]

                # 4. 投信作帳指標 (MA20/MA60)
                df_5m['ma20'] = df_5m['Close'].rolling(20).mean()
                df_5m['ma60'] = df_5m['Close'].rolling(60).mean()
                vol_avg = df_5m['Volume'].rolling(20).mean()
                is_it_buy = (df_5m['Volume'] > vol_avg * 1.5) & (df_5m['Close'] > df_5m['Open']) & (df_5m['Close'] > df_5m['ma20'])
                df_5m['it_buy_rolling_count'] = is_it_buy.rolling(5).sum().fillna(0)
                
                last = df_5m.iloc[-1]
                results.append({
                    "ticker": ticker,
                    "status": "SQUEEZING" if last["sqz_on"] else "FIRED",
                    "pattern": pattern_status,
                    "pivot": pivot_price,
                    "close": last["Close"],
                    "score": last.get("score", 0),
                    "it_buy": "🔥 強力建倉" if last["it_buy_rolling_count"] >= 2 else "⚪ 中性"
                })
            except Exception as e:
                print(f"⚠️ Failed to scan {ticker}: {e}")
        return pd.DataFrame(results)
