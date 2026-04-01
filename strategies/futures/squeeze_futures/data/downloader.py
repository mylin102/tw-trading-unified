import logging
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

def download_futures_data(ticker: str, interval: str = "5m", period: str = "5d") -> pd.DataFrame:
    """
    下載期貨或指數的即時/盤中數據。
    
    Args:
        ticker: 代號 (例如 '^TWII', '0050.TW', 'NQ=F')
        interval: 時間週期 ('1m', '5m', '15m', '60m', '1d')
        period: 抓取長度 ('1d', '5d', '1mo')
    """
    try:
        logger.info(f"Downloading {ticker} data (interval: {interval}, period: {period})...")
        df = yf.download(
            tickers=ticker,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=True
        )
        
        if df.empty:
            logger.warning(f"No data returned for {ticker}")
            return pd.DataFrame()
            
        # 整理欄位
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
            
        return df
        
    except Exception as e:
        logger.error(f"Error downloading {ticker}: {str(e)}")
        return pd.DataFrame()

def get_multi_timeframe_data(ticker: str, timeframes: list[str] = ["5m", "15m", "1h"]) -> dict[str, pd.DataFrame]:
    """
    獲取多週期的數據用於共振分析。
    """
    results = {}
    for tf in timeframes:
        # 根據 timeframe 調整 period
        period = "5d" if tf in ["1m", "5m"] else "1mo"
        df = download_futures_data(ticker, interval=tf, period=period)
        if not df.empty:
            results[tf] = df
    return results
