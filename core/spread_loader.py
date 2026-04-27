"""
Spread Loader — loads far-month futures price data from CSV,
computes near-far spread and z-scores, and enriches bar dicts.

This is the bridge between CSV-stored far-month data (fetched by
fetch_far_month_data.py) and the live bar pipeline. Since PAPI
returns empty kbars for far-month contracts, we rely on CSV data
updated periodically via real-account API calls.

Usage:
    loader = SpreadLoader()
    loader.load_far_csv()           # Load latest CSV
    loader.load_near_csv()          # Optional: override near close
    bar = loader.enrich_bar(bar)    # Add spread_z, vwap_z, etc. to bar dict
"""

from __future__ import annotations

import os
import glob
import logging
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Paths relative to project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class SpreadLoader:
    """Loads far-month data from CSV and computes spread metrics for bar enrichment."""

    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir or DATA_DIR
        self._far_df: pd.DataFrame | None = None   # far-month price series (from CSV)
        self._near_df: pd.DataFrame | None = None  # near-month price series (optional override)
        self._last_spread_data: dict[str, Any] = {}  # cached latest spread values
        self._csv_paths: dict[str, str] = {}  # { 'far': path, 'near': path }

    # ── Public API ──────────────────────────────────────────────────────

    def load_latest_csv(self) -> bool:
        """Find and load the latest CSV files.

        Priority:
          1. mxf_calendar_spread_*.csv (has pre-computed spread_z, vwap_z)
          2. mxf_far_*.csv + mxf_near_*.csv (raw data, compute spread_z at load)

        Returns True if at least some data was loaded.
        """
        # Try calendar spread CSV first (most complete)
        ok_spread = self._find_and_load("spread")
        if ok_spread:
            return True

        # Fallback: far + near
        ok_far = self._find_and_load("far")
        ok_near = self._find_and_load("near")
        return ok_far is True  # near is optional

    def get_spread_z(self, timestamp: pd.Timestamp | None = None) -> float | None:
        """Get the spread z-score at a given timestamp (or latest)."""
        if self._far_df is None or self._far_df.empty:
            return None
        ts = timestamp or self._far_df.index[-1]
        row = self._get_row_at(ts)
        if row is None or pd.isna(row.get("spread_z")):
            return None
        return float(row["spread_z"])

    def enrich_bar(self, bar: dict[str, Any], timestamp: pd.Timestamp | None = None) -> dict[str, Any]:
        """Enrich a bar dict with spread-related fields.

        Adds these keys (or fills defaults):
          - spread_z, near_close, far_close, price_vs_vwap, vwap_z
          - bars_from_session_open, is_night_session

        Returns the same dict (mutated in place + returned).
        """
        if self._far_df is None or self._far_df.empty:
            self._fill_default_spread(bar, timestamp)
        else:
            ts = timestamp or self._find_bar_timestamp(bar)
            row = self._get_row_at(ts)
            if row is not None and not pd.isna(row.get("spread_z", np.nan)):
                bar["spread_z"] = float(row["spread_z"])
                bar["near_close"] = float(row.get("Close_near", bar.get("Close", 0.0)))
                bar["far_close"] = float(row.get("Close_far", 0.0))
                # spread_age_minutes: how old is the CSV data (for staleness gate)
                if self._far_df is not None and not self._far_df.empty:
                    try:
                        last_csv_ts = self._far_df.index[-1]
                        age_delta = pd.Timestamp.now(tz=last_csv_ts.tz) - last_csv_ts
                        bar["spread_age_minutes"] = int(age_delta.total_seconds() / 60)
                    except Exception:
                        bar["spread_age_minutes"] = None
                else:
                    bar["spread_age_minutes"] = None
            else:
                self._fill_default_spread(bar, timestamp)
                bar["spread_age_minutes"] = None

        # vwap_z: use from CSV if available (calendar spread CSV has it)
        if "vwap_z" in row.index and not pd.isna(row.get("vwap_z", np.nan)):
            bar["vwap_z"] = float(row["vwap_z"])
        elif "vwap_z" not in bar or bar["vwap_z"] is None:
            bar["vwap_z"] = self._calc_vwap_z(bar)

        # price_vs_vwap: normalized price deviation
        if "price_vs_vwap" not in bar or bar["price_vs_vwap"] is None:
            bar["price_vs_vwap"] = self._calc_price_vs_vwap(bar)

        # bars_from_session_open
        if "bars_from_session_open" not in bar or bar["bars_from_session_open"] is None:
            bar["bars_from_session_open"] = self._calc_bars_from_open(bar, timestamp)

        # is_night_session
        if "is_night_session" not in bar or bar["is_night_session"] is None:
            bar["is_night_session"] = self._calc_is_night(bar, timestamp)

        # also ensure breakout_strength / volume_spike / regime / adx exist
        bar.setdefault("breakout_strength", 0.0)
        bar.setdefault("volume_spike", 1.0)
        bar.setdefault("regime", "WEAK")
        bar.setdefault("adx", 15.0)

        return bar

    # ── Loading ─────────────────────────────────────────────────────────

    def _find_and_load(self, prefix: str) -> bool:
        """Find CSV matching the prefix and load it.

        For prefix='spread': matches mxf_calendar_spread_*.csv
        For prefix='far'/'near': matches mxf_{prefix}_*.csv
        """
        if prefix == "spread":
            pattern = os.path.join(self.data_dir, "mxf_calendar_spread_*.csv")
        else:
            pattern = os.path.join(self.data_dir, f"mxf_{prefix}_*.csv")
        files = sorted(glob.glob(pattern))
        if not files:
            logger.warning(f"[SpreadLoader] No CSV files found for prefix={prefix} in {self.data_dir}")
            return False

        csv_path = files[-1]  # latest by filename sort
        self._csv_paths[prefix] = csv_path
        df = pd.read_csv(csv_path)

        # Normalize timestamp column
        ts_col = None
        for candidate in ["ts", "timestamp", "datetime"]:
            if candidate in df.columns:
                ts_col = candidate
                break

        if ts_col is None:
            logger.warning(f"[SpreadLoader] No timestamp column in {csv_path}: cols={list(df.columns)}")
            return False

        df = df.copy()
        # Handle nanosecond timestamps
        sample = df[ts_col].iloc[0]
        if isinstance(sample, (int, float)) and sample > 1e15:
            # nanosecond epoch
            df[ts_col] = pd.to_datetime(df[ts_col], unit="ns", errors="coerce")
        else:
            df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce")

        df = df.dropna(subset=[ts_col]).set_index(ts_col)
        df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index

        if prefix == "spread":
            # Calendar spread CSV has pre-computed spread_z, vwap_z
            self._far_df = df  # store in _far_df for lookup
            logger.info(f"[SpreadLoader] Loaded calendar spread: {csv_path} ({len(df)} rows, "
                        f"cols={list(df.columns)})")
        elif prefix == "far":
            self._far_df = df
            logger.info(f"[SpreadLoader] Loaded far: {csv_path} ({len(df)} rows)")
        else:
            self._near_df = df

        return True

    # ── Internal helpers ────────────────────────────────────────────────

    def _find_bar_timestamp(self, bar: dict) -> pd.Timestamp:
        """Extract a pd.Timestamp from the bar dict."""
        for key in ("ts", "timestamp", "datetime", "time"):
            val = bar.get(key)
            if val is not None:
                try:
                    return pd.Timestamp(val)
                except Exception:
                    continue
        return pd.Timestamp.now()

    def _get_row_at(self, ts: pd.Timestamp) -> pd.Series | None:
        """Find the nearest row in far_df at or before ts."""
        if self._far_df is None or self._far_df.empty:
            return None
        ts = pd.Timestamp(ts).tz_localize(None)
        idx = self._far_df.index.searchsorted(ts, side="right") - 1
        if idx < 0:
            return None
        return self._far_df.iloc[idx]

    def _fill_default_spread(self, bar: dict, ts: pd.Timestamp | None) -> None:
        """Set spread_z to a safe default (0.0 = neutral)."""
        bar["spread_z"] = 0.0
        bar["near_close"] = bar.get("Close", 0.0)
        bar["far_close"] = bar.get("Close", 0.0)

    def _calc_vwap_z(self, bar: dict) -> float:
        """Calculate (close - vwap) / atr, a normalized version of vwap_z."""
        close = bar.get("Close", 0.0) or 0.0
        vwap = bar.get("vwap", close) or close
        atr = bar.get("atr", 1.0) or 1.0
        if vwap == 0 or atr <= 0:
            return 0.0
        return float((close - vwap) / (atr * 5))  # scale: ±1 ~ ±5 atr

    def _calc_price_vs_vwap(self, bar: dict) -> float:
        """Normalized price vs VWAP: (close - vwap) / close * 100."""
        close = bar.get("Close", 0.0) or 0.0
        vwap = bar.get("vwap", close) or close
        if close == 0:
            return 0.0
        return float((close - vwap) / close * 100)

    def _calc_bars_from_open(self, bar: dict, ts: pd.Timestamp | None) -> int:
        """Estimate bars from session open (0 for first bar)."""
        ts = ts or self._find_bar_timestamp(bar)
        hour = ts.hour
        minute = ts.minute

        # Day session opens at 08:45, night at 15:00
        if 8 <= hour < 14 or (hour == 8 and minute >= 45):
            # Day session
            open_minutes = (hour * 60 + minute) - (8 * 60 + 45)
        elif hour >= 15 or hour < 5:
            # Night session: starts at 15:00
            if hour >= 15:
                open_minutes = (hour * 60 + minute) - (15 * 60)
            else:
                open_minutes = (hour * 60 + minute) + (24 * 60 - 15 * 60)
        else:
            open_minutes = 0

        return max(0, int(open_minutes / 5))  # 5-min bars

    def _calc_is_night(self, bar: dict, ts: pd.Timestamp | None) -> bool:
        """Detect night session."""
        ts = ts or self._find_bar_timestamp(bar)
        hour = ts.hour
        return hour >= 15 or hour < 5

    # ── Cache ───────────────────────────────────────────────────────────

    def clear_cache(self) -> None:
        self._far_df = None
        self._near_df = None
        self._csv_paths = {}

    def status(self) -> dict[str, Any]:
        return {
            "csv": self._csv_paths.get("spread") or self._csv_paths.get("far"),
            "near_csv": self._csv_paths.get("near"),
            "rows": len(self._far_df) if self._far_df is not None else 0,
            "near_rows": len(self._near_df) if self._near_df is not None else 0,
            "last_spread_z": self._last_spread_data.get("spread_z"),
        }


# Singleton for use across the system
_shared_loader: SpreadLoader | None = None


def get_spread_loader() -> SpreadLoader:
    """Get or create the shared SpreadLoader singleton."""
    global _shared_loader
    if _shared_loader is None:
        _shared_loader = SpreadLoader()
    return _shared_loader
