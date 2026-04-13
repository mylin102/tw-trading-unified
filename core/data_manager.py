"""
Data Manager — Centralized access to historical Parquet database.
Provides inventory stats and high-performance loading.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, Any, Optional

import pandas as pd


class DataManager:
    """Manages the historical backtest database (Parquet)."""

    def __init__(self, base_path: str = "data/historical"):
        self.base_path = Path(base_path)
        self.base_path.mkdir(parents=True, exist_ok=True)

    def get_path(self, ticker: str) -> Path:
        """Return the Parquet file path for a given ticker."""
        return self.base_path / f"{ticker.replace('.', '_')}_5m.parquet"

    def load_historical(self, ticker: str) -> pd.DataFrame:
        """Load historical bars from Parquet."""
        path = self.get_path(ticker)
        if not path.exists():
            return pd.DataFrame()
        
        try:
            df = pd.read_parquet(path)
            # Ensure index is datetime and sorted
            if not pd.api.types.is_datetime64_any_dtype(df.index):
                df.index = pd.to_datetime(df.index)
            return df.sort_index()
        except Exception as e:
            print(f"Error loading {ticker}: {e}")
            return pd.DataFrame()

    def get_inventory(self) -> Dict[str, Dict[str, Any]]:
        """Scan data directory and return statistics for all available tickers."""
        stats = {}
        for p in self.base_path.glob("*.parquet"):
            ticker = p.stem.replace("_5m", "").replace("_", ".")
            try:
                df = pd.read_parquet(p)
                if df.empty:
                    continue
                
                stats[ticker] = {
                    "start": df.index.min(),
                    "end": df.index.max(),
                    "rows": len(df),
                    "size_mb": round(os.path.getsize(p) / (1024 * 1024), 2),
                    "path": str(p)
                }
            except:
                continue
        return stats

    def save_historical(self, ticker: str, df: pd.DataFrame):
        """Save historical bars to Parquet atomically."""
        if df.empty:
            return
        
        path = self.get_path(ticker)
        tmp_path = path.with_suffix(".tmp.parquet")
        
        # Ensure it's sorted and no duplicates before saving
        df = df.sort_index()
        df = df[~df.index.duplicated(keep='first')]
        
        # Write to temporary file first
        df.to_parquet(tmp_path, compression='snappy')
        
        # Atomic rename (replace) ensures no partial reads
        os.replace(tmp_path, path)

# Singleton
data_manager = DataManager()
