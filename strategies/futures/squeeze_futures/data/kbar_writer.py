#!/usr/bin/env python3
"""
RawKbarWriter - append-only CSV for raw API kbar data.

Design:
  - Every api.kbars() response is saved to CSV BEFORE any processing.
  - Raw CSV is the truth source; re-processing from raw kbars reproduces indicators.
  - Stratup can rebuild canonical bars from raw CSV instead of calling API again.

Directory: logs/raw_kbars/{contract}_{YYYYMMDD}_kbars.csv
Fields:    ts, Open, High, Low, Close, Volume, Amount, trading_day
"""

import os
import csv
from datetime import datetime
from pathlib import Path
from typing import Optional
import pandas as pd


class RawKbarWriter:
    """Append-only CSV writer for raw API kbar responses."""

    def __init__(self, contract_code: str, trading_day: str):
        self._contract_code = contract_code
        self._trading_day = trading_day
        self._file_path: Optional[Path] = None
        self._file_handle = None
        self._csv_writer = None
        self._record_count = 0
        self._open()

    def _open(self):
        """Open (or create) the CSV file for appending."""
        base = Path(__file__).resolve().parents[4]  # project root
        raw_dir = base / "logs" / "raw_kbars"
        raw_dir.mkdir(parents=True, exist_ok=True)

        self._file_path = raw_dir / f"{self._contract_code}_{self._trading_day}_kbars.csv"
        file_exists = self._file_path.exists()

        self._file_handle = open(self._file_path, "a", newline="")
        self._csv_writer = csv.writer(self._file_handle)

        if not file_exists:
            self._csv_writer.writerow([
                "ts", "Open", "High", "Low", "Close",
                "Volume", "Amount", "trading_day"
            ])
            self._file_handle.flush()

    def write_kbar(self, ts: str, ohlcva: dict) -> None:
        """Append a single kbar row. Must be called before any computation."""
        try:
            row = [
                ts,
                float(ohlcva.get("Open", 0)),
                float(ohlcva.get("High", 0)),
                float(ohlcva.get("Low", 0)),
                float(ohlcva.get("Close", 0)),
                int(ohlcva.get("Volume", 0)),
                float(ohlcva.get("Amount", 0)),
                self._trading_day,
            ]
            self._csv_writer.writerow(row)
            self._record_count += 1

            # Flush every 100 records
            if self._record_count % 100 == 0:
                self._file_handle.flush()

        except Exception:
            pass

    def write_dataframe(self, df: pd.DataFrame) -> None:
        """Write an entire DataFrame of kbars (raw API response)."""
        if df is None or df.empty:
            return
        try:
            # Determine the timestamp column: "ts" index or "ts" col
            if isinstance(df.index, pd.DatetimeIndex):
                timestamps = df.index
            elif "ts" in df.columns:
                timestamps = pd.to_datetime(df["ts"])
            else:
                return

            for i, ts_val in enumerate(timestamps):
                row_df = df.iloc[i]
                ts_str = ts_val.isoformat() if hasattr(ts_val, "isoformat") else str(ts_val)
                ohlcva = {
                    "Open": getattr(row_df, "Open", row_df.get("Open", 0)),
                    "High": getattr(row_df, "High", row_df.get("High", 0)),
                    "Low": getattr(row_df, "Low", row_df.get("Low", 0)),
                    "Close": getattr(row_df, "Close", row_df.get("Close", 0)),
                    "Volume": getattr(row_df, "Volume", row_df.get("Volume", 0)),
                    "Amount": getattr(row_df, "Amount", row_df.get("Amount", 0)),
                }
                self.write_kbar(ts_str, ohlcva)

            self._file_handle.flush()
        except Exception:
            pass

    def flush(self) -> None:
        if self._file_handle and not self._file_handle.closed:
            self._file_handle.flush()

    def close(self) -> None:
        if self._file_handle and not self._file_handle.closed:
            self._file_handle.close()
            self._file_handle = None

    @property
    def record_count(self) -> int:
        return self._record_count

    def __del__(self):
        self.close()


def read_raw_kbars(contract_code: str, trading_day: str) -> pd.DataFrame:
    """Read a day's raw kbars from CSV. Returns empty DataFrame if missing."""
    base = Path(__file__).resolve().parents[4]
    path = base / "logs" / "raw_kbars" / f"{contract_code}_{trading_day}_kbars.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        if "ts" in df.columns:
            df["ts"] = pd.to_datetime(df["ts"])
            df = df.set_index("ts")
        return df
    except Exception:
        return pd.DataFrame()
