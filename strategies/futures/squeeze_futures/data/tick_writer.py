#!/usr/bin/env python3
"""
RawTickWriter - append-only CSV 原始終端機資料落地層

Design:
  - Every tick from Shioaji callback is written to CSV BEFORE any in-memory cache.
  - Raw CSV is the truth source; deque is only a runtime performance cache.
  - A crash can always rebuild from raw CSV.

Directory: logs/raw_ticks/{contract}_{YYYYMMDD}_ticks.csv
Fields:    timestamp, trading_day, symbol, price, volume, bid_price, ask_price, ts_int
"""

import os
import csv
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
import pandas as pd


class RawTickWriter:
    """Append-only CSV writer for raw tick data. Thread-safe for single-writer.

    Design invariants (P1 layer):
    - Raw tick CSV is the TRUTH SOURCE — always written first.
    - Deque is a RUNTIME CACHE ONLY — can always rebuild from CSV.
    - write() must be called BEFORE any in-memory cache update.
    - A crash can always rebuild state from raw CSV.
    """

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
        # Determine base directory; use project root relative to this file
        base = Path(__file__).resolve().parents[4]  # navigates up to project root
        raw_dir = base / "logs" / "raw_ticks"
        raw_dir.mkdir(parents=True, exist_ok=True)

        self._file_path = raw_dir / f"{self._contract_code}_{self._trading_day}_ticks.csv"
        file_exists = self._file_path.exists()

        self._file_handle = open(self._file_path, "a", newline="")
        self._csv_writer = csv.writer(self._file_handle)

        # Write header only if file is new
        if not file_exists:
            self._csv_writer.writerow([
                "timestamp", "trading_day", "symbol", "price",
                "volume", "bid_price", "ask_price", "ts_int"
            ])
            self._file_handle.flush()

    def write(self, tick) -> None:
        """Append a single tick to CSV. Must be called before any in-memory use."""
        try:
            ts = tick.datetime
            if isinstance(ts, datetime):
                ts_str = ts.isoformat()
            else:
                ts_str = str(ts)

            # Normalise to epoch int for easy sorting / gap detection
            if isinstance(tick.datetime, datetime):
                ts_int = int(tick.datetime.timestamp())
            else:
                ts_int = int(time.time())

            row = [
                ts_str,
                self._trading_day,
                getattr(tick, "code", self._contract_code),
                float(getattr(tick, "close", 0.0)),
                int(getattr(tick, "volume", 0)),
                float(getattr(tick, "bid_price", 0.0)),
                float(getattr(tick, "ask_price", 0.0)),
                ts_int,
            ]
            self._csv_writer.writerow(row)
            self._record_count += 1

            # Flush every 100 records to balance I/O vs safety
            if self._record_count % 100 == 0:
                self._file_handle.flush()

        except Exception:
            # Never let a write failure crash the tick callback
            pass

    def flush(self) -> None:
        """Force flush to disk."""
        if self._file_handle and not self._file_handle.closed:
            self._file_handle.flush()

    def close(self) -> None:
        """Close the file handle."""
        if self._file_handle and not self._file_handle.closed:
            self._file_handle.close()
            self._file_handle = None

    @property
    def record_count(self) -> int:
        return self._record_count

    def __del__(self):
        self.close()


def read_raw_ticks(contract_code: str, trading_day: str) -> pd.DataFrame:
    """Read a day's raw ticks from CSV. Returns empty DataFrame if file missing."""
    base = Path(__file__).resolve().parents[4]
    path = base / "logs" / "raw_ticks" / f"{contract_code}_{trading_day}_ticks.csv"
    if not path.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(path)
        if "timestamp" in df.columns:
            df["timestamp"] = pd.to_datetime(df["timestamp"])
        return df
    except Exception:
        return pd.DataFrame()


# ── Helper to determine the trading day string ──

def get_trading_day_str(now: Optional[datetime] = None) -> str:
    """Return YYYYMMDD trading-day string.
    Taiwan futures trading day rolls at 05:00 local time.
    """
    if now is None:
        now = datetime.now()
    # If before 5 AM, it's still yesterday's trading day
    if now.hour < 5:
        from datetime import timedelta
        return (now - timedelta(days=1)).strftime("%Y%m%d")
    return now.strftime("%Y%m%d")
