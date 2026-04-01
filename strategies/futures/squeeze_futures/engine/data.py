#!/usr/bin/env python3
"""
數據管理模組 (Data Manager)
負責載入、處理和管理市場數據

靈感來自 vectorbt-pro 的 Data 模組
"""

import pandas as pd
import numpy as np
from pathlib import Path
from typing import Dict, Optional, List, Union
from rich.console import Console

console = Console()


class DataManager:
    """
    數據管理器
    
    功能：
    - 載入多種數據源 (CSV, Yahoo Finance, 資料庫)
    - 數據清洗與標準化
    - 多時間框架數據管理
    - 指標計算與緩存
    """
    
    def __init__(self, data_dir: str = "data/taifex_raw"):
        """
        Args:
            data_dir: 數據目錄路徑
        """
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        
        self._data_cache: Dict[str, pd.DataFrame] = {}
        self._indicator_cache: Dict[str, pd.DataFrame] = {}
    
    def load_csv(self, filename: str, **kwargs) -> pd.DataFrame:
        """
        載入 CSV 文件
        
        Args:
            filename: 檔案名稱
            **kwargs: 傳遞給 pd.read_csv 的參數
        
        Returns:
            DataFrame with OHLCV data
        """
        filepath = self.data_dir / filename
        
        if not filepath.exists():
            # 嘗試在子目錄中查找
            for pattern in self.data_dir.glob(f"**/{filename}"):
                filepath = pattern
                break
        
        console.print(f"[dim]載入數據：{filepath}[/dim]")
        
        df = pd.read_csv(filepath, index_col=0, parse_dates=True, **kwargs)
        
        # 標準化欄位名稱
        df = self._standardize_columns(df)
        
        console.print(f"[green]載入 {len(df)} 筆數據[/green]")
        return df
    
    def load_yahoo(self, ticker: str, period: str = "60d", interval: str = "5m") -> pd.DataFrame:
        """
        從 Yahoo Finance 載入數據
        
        Args:
            ticker: 代碼 (e.g., '^TWII')
            period: 期間 (1d, 5d, 1mo, 3mo, 6mo, 1y, 2y, 5y, 10y, ytd, max)
            interval: 週期 (1m, 2m, 5m, 15m, 30m, 60m, 90m, 1h, 1d, 5d, 1wk, 1mo, 3mo)
        
        Returns:
            DataFrame with OHLCV data
        """
        cache_key = f"{ticker}_{period}_{interval}"
        
        if cache_key in self._data_cache:
            console.print(f"[dim]使用緩存數據：{cache_key}[/dim]")
            return self._data_cache[cache_key].copy()
        
        try:
            import yfinance as yf
            
            console.print(f"[dim]從 Yahoo Finance 下載：{ticker} ({interval}, {period})[/dim]")
            
            ticker_obj = yf.Ticker(ticker)
            df = ticker_obj.history(period=period, interval=interval)
            
            if df.empty:
                console.print("[yellow]未獲取到數據[/yellow]")
                return pd.DataFrame()
            
            # 標準化
            df = self._standardize_columns(df)
            df = df.dropna()
            
            # 緩存
            self._data_cache[cache_key] = df.copy()
            
            console.print(f"[green]載入 {len(df)} 筆數據[/green]")
            return df
            
        except ImportError:
            console.print("[yellow]yfinance 未安裝[/yellow]")
            return pd.DataFrame()
        except Exception as e:
            console.print(f"[red]下載錯誤：{e}[/red]")
            return pd.DataFrame()
    
    def load_multiple_timeframes(
        self,
        ticker: str,
        timeframes: List[str] = ["5m", "15m", "1h"],
        period: str = "60d",
    ) -> Dict[str, pd.DataFrame]:
        """
        載入多時間框架數據
        
        Args:
            ticker: 代碼
            timeframes: 時間框架列表
            period: 期間
        
        Returns:
            Dict[timeframe, DataFrame]
        """
        data = {}
        
        for tf in timeframes:
            df = self.load_yahoo(ticker, period=period, interval=tf)
            if not df.empty:
                data[tf] = df
        
        console.print(f"[green]載入 {len(data)} 個時間框架數據[/green]")
        return data
    
    def _standardize_columns(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        標準化欄位名稱
        
        Args:
            df: 原始 DataFrame
        
        Returns:
            標準化後的 DataFrame
        """
        # 欄位映射
        column_map = {
            'Open': 'Open',
            'High': 'High',
            'Low': 'Low',
            'Close': 'Close',
            'Volume': 'Volume',
            'Adj Close': 'Adj_Close',
            'Dividends': 'Dividends',
            'Stock Splits': 'Stock_Splits',
        }
        
        # 重命名
        df = df.rename(columns=column_map)
        
        # 確保必要的欄位存在
        required = ['Open', 'High', 'Low', 'Close', 'Volume']
        for col in required:
            if col not in df.columns:
                if col.lower() in df.columns.str.lower():
                    # 尋找大小寫不符的欄位
                    for c in df.columns:
                        if c.lower() == col.lower():
                            df = df.rename(columns={c: col})
                            break
                else:
                    df[col] = np.nan
        
        return df
    
    def add_indicators(
        self,
        df: pd.DataFrame,
        indicators: List[str] = None,
        cache_key: str = None,
    ) -> pd.DataFrame:
        """
        添加技術指標
        
        Args:
            df: OHLCV 數據
            indicators: 指標列表 ['vwap', 'squeeze', 'ema', ...]
            cache_key: 緩存鍵
        
        Returns:
            DataFrame with indicators
        """
        from squeeze_futures.engine.indicators import (
            calculate_futures_squeeze,
            calculate_atr,
        )
        
        if cache_key and cache_key in self._indicator_cache:
            return self._indicator_cache[cache_key].copy()
        
        result = df.copy()
        
        # 預設指標
        if indicators is None:
            indicators = ['squeeze']
        
        # 計算 Squeeze 指標
        if 'squeeze' in indicators:
            console.print("[dim]計算 Squeeze 指標...[/dim]")
            result = calculate_futures_squeeze(result)
        
        # 計算 ATR
        if 'atr' in indicators:
            console.print("[dim]計算 ATR...[/dim]")
            result['atr'] = calculate_atr(result)
        
        # 計算 VWAP (如果尚未存在)
        if 'vwap' not in result.columns and 'Volume' in result.columns:
            console.print("[dim]計算 VWAP...[/dim]")
            result['date'] = result.index.date
            typical_price = (result['High'] + result['Low'] + result['Close']) / 3
            result['vwap'] = (typical_price * result['Volume']).groupby(result['date']).cumsum() / result['Volume'].groupby(result['date']).cumsum()
            result = result.drop(columns=['date'])
        
        # 緩存
        if cache_key:
            self._indicator_cache[cache_key] = result.copy()
        
        return result
    
    def save(self, df: pd.DataFrame, filename: str):
        """
        保存數據到 CSV
        
        Args:
            df: 數據 DataFrame
            filename: 檔案名稱
        """
        filepath = self.data_dir / filename
        df.to_csv(filepath)
        console.print(f"[green]數據已保存至：{filepath}[/green]")
    
    def clear_cache(self):
        """清除所有緩存"""
        self._data_cache.clear()
        self._indicator_cache.clear()
        console.print("[dim]緩存已清除[/dim]")
    
    def get_cache_info(self) -> Dict[str, int]:
        """獲取緩存信息"""
        return {
            'data_cache': len(self._data_cache),
            'indicator_cache': len(self._indicator_cache),
        }
