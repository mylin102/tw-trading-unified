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
import re
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from core.runtime_paths import runtime_path

logger = logging.getLogger(__name__)

# Paths relative to project root
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")


class SpreadLoader:
    """Loads far-month data from CSV and computes spread metrics for bar enrichment."""

    def __init__(self, data_dir: str | None = None):
        self.data_dir = data_dir if data_dir is not None else runtime_path("data")
        self._far_df: pd.DataFrame | None = None   # far-month price series (from CSV)
        self._near_df: pd.DataFrame | None = None  # near-month price series (optional override)
        self._last_spread_data: dict[str, Any] = {}  # cached latest spread values
        self._csv_paths: dict[str, str] = {}  # { 'far': path, 'near': path }
        self._spread_csv_mtime: float | None = None  # for hot-reload detection
        self._spread_fingerprint: tuple | None = None
        self._last_refresh_error: str | None = None
        self._last_successful_refresh: str | None = None
        # 2026-06-26 Gemini CLI: Store active ticker to eliminate silent default fallbacks
        self._ticker: str | None = None

    # ── Public API ──────────────────────────────────────────────────────

    def load_latest_csv(self, ticker: str) -> bool:
        """Find and load the latest CSV files.

        Priority:
          1. {ticker}_calendar_spread_*.csv (has pre-computed spread_z, vwap_z)
          2. {ticker}_far_*.csv + {ticker}_near_*.csv (raw data, compute spread_z at load)

        Returns True if at least some data was loaded.
        """
        # 2026-06-26 Gemini CLI: enforce explicit non-empty ticker check
        if not ticker:
            raise ValueError("[SpreadLoader] Ticker cannot be None or empty. Silent fallbacks are forbidden.")
        self._ticker = ticker.lower()

        # Try calendar spread CSV first (most complete)
        candidates = self._spread_candidates(self._ticker)
        ok_spread = self._find_and_load("spread", self._ticker)
        if ok_spread:
            return True

        # A present but invalid formal file must not silently select older raw inputs.
        if candidates:
            return False

        # Fallback: far + near
        ok_far = self._find_and_load("far", self._ticker)
        ok_near = self._find_and_load("near", self._ticker)
        return ok_far is True  # near is optional

    def get_spread_z(self, timestamp: pd.Timestamp | None = None) -> float | None:
        """Get the spread z-score at a given timestamp (or latest)."""
        self._check_reload()
        row = None
        if self._far_df is None or self._far_df.empty:
            return None
        ts = timestamp or self._far_df.index[-1]
        row = self._get_row_at(ts)
        if row is None or pd.isna(row.get("spread_z")):
            return None
        return float(row["spread_z"])

    def enrich_bar(self, bar: dict[str, Any], timestamp: pd.Timestamp | None = None) -> dict[str, Any]:
        """Enrich a bar dict with spread-related fields.
        
        Auto-reloads CSV if a newer file is detected (hot-reload support for cron updates).
        
        Adds these keys (or fills defaults):
          - spread_z, near_close, far_close, price_vs_vwap, vwap_z
          - bars_from_session_open, is_night_session

        Returns the same dict (mutated in place + returned).
        """
        # ── [Hot-reload] Check for newer CSV on every bar ──
        self._check_reload()
        if self._far_df is None or self._far_df.empty:
            self._fill_default_spread(bar, timestamp)
        else:
            ts = timestamp or self._find_bar_timestamp(bar)
            row = self._get_row_at(ts)
            if row is not None:
                for key in ("spread_z", "spread_ma", "spread_std"):
                    if key not in row.index:
                        continue
                    value = row.get(key, np.nan)
                    bar[key] = None if pd.isna(value) else float(value)
                # 2026-07-09 Hermes Agent: Enrich Spread EMA 20 and EMA 60 values
                bar["spread_ema_20"] = float(row.get("spread_ema_20", 0.0))
                bar["spread_ema_60"] = float(row.get("spread_ema_60", 0.0))
                # 2026-06-26 Gemini CLI: Prioritize the bar's actual Close price over the loaded CSV to prevent stale price overrides
                bar["near_close"] = float(bar.get("Close", bar.get("close", row.get("Close_near", 0.0))))
                # 2026-07-02 Hermes Agent: Preserve existing far_close if bar already has one
                # (e.g. from far_close_rt injected by _strategy_tick), preventing CSV stale data override
                if "far_close" not in bar or bar["far_close"] is None or bar["far_close"] <= 0:
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
        if row is not None and "vwap_z" in row.index and not pd.isna(row.get("vwap_z", np.nan)):
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

    def _check_reload(self) -> None:
        """Reload the newest valid formal CSV or a changed atomic replacement."""
        if not self._ticker:
            spread_path = self._csv_paths.get("spread")
            if spread_path and os.path.exists(spread_path):
                current_mtime = os.path.getmtime(spread_path)
                if current_mtime != self._spread_csv_mtime:
                    raise ValueError("[SpreadLoader] Cannot hot-reload because no ticker is cached in the loader.")
            return
        candidates = self._spread_candidates(self._ticker)
        if not candidates:
            if self._csv_paths.get("spread"):
                self._last_refresh_error = "No valid formal calendar spread CSV found"
            return
        spread_path = candidates[-1][1]
        fingerprint = self._file_fingerprint(spread_path)
        if (spread_path == self._csv_paths.get("spread")
                and fingerprint == self._spread_fingerprint):
            return
        self._find_and_load("spread", self._ticker)

    def _spread_candidates(self, ticker: str) -> list[tuple[datetime, str]]:
        pattern = re.compile(
            rf"^{re.escape(ticker.lower())}_calendar_spread_(\d{{8}})\.csv$"
        )
        candidates = []
        try:
            names = os.listdir(self.data_dir)
        except OSError:
            return candidates
        for name in names:
            match = pattern.fullmatch(name)
            if not match:
                continue
            try:
                day = datetime.strptime(match.group(1), "%Y%m%d")
            except ValueError:
                continue
            path = os.path.join(self.data_dir, name)
            if os.path.isfile(path):
                candidates.append((day, path))
        return sorted(candidates)

    @staticmethod
    def _file_fingerprint(path: str) -> tuple:
        stat = os.stat(path)
        return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns

    @staticmethod
    def _validate_spread_frame(df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            raise ValueError("calendar spread CSV is empty")
        ts_col = next((c for c in ("ts", "timestamp", "datetime") if c in df.columns), None)
        required = ("Close_near", "Close_far", "spread", "spread_ma", "spread_std", "spread_z")
        missing = [c for c in required if c not in df.columns]
        if ts_col is None or missing:
            raise ValueError(f"calendar spread schema invalid: timestamp={ts_col}, missing={missing}")
        parsed_ts = pd.to_datetime(df[ts_col], errors="coerce")
        if parsed_ts.isna().any():
            raise ValueError("calendar spread timestamp invalid")
        df = df.copy()
        for column in required:
            original = df[column]
            numeric = pd.to_numeric(original, errors="coerce")
            malformed = numeric.isna() & original.notna() & original.astype(str).str.strip().ne("")
            if malformed.any():
                raise ValueError(f"calendar spread numeric field invalid: {column}")
            if column in ("Close_near", "Close_far", "spread") and numeric.isna().any():
                raise ValueError(f"calendar spread required quote missing: {column}")
            if numeric.notna().any() and not np.isfinite(numeric.dropna()).all():
                raise ValueError(f"calendar spread numeric field non-finite: {column}")
            df[column] = numeric
        df[ts_col] = parsed_ts
        df = df.set_index(ts_col)
        if df.index.has_duplicates or not df.index.is_monotonic_increasing:
            raise ValueError("calendar spread timestamps must be unique and ordered")
        rows = np.arange(len(df))
        warmup_or_zero_std = (rows < 19) | df["spread_std"].eq(0).to_numpy()
        for column in ("spread_ma", "spread_std"):
            invalid = df[column].isna().to_numpy() & (rows >= 19)
            if invalid.any():
                raise ValueError(f"calendar spread rolling field incomplete: {column}")
        invalid_z = df["spread_z"].isna().to_numpy() & ~warmup_or_zero_std
        if invalid_z.any():
            raise ValueError("calendar spread Z missing after warmup with nonzero std")
        zero_std_has_z = df["spread_z"].notna().to_numpy() & df["spread_std"].eq(0).to_numpy()
        if zero_std_has_z.any():
            raise ValueError("calendar spread Z must be undefined when std is zero")
        return df

    def _find_and_load(self, prefix: str, ticker: str | None = None) -> bool:
        """Find CSV matching the prefix and load it.

        For prefix='spread': matches {ticker}_calendar_spread_*.csv
        For prefix='far'/'near': matches {ticker}_{prefix}_*.csv
        """
        # 2026-06-26 Gemini CLI: strictly enforce ticker presence to avoid default "mxf" fallback
        if not ticker:
            raise ValueError("[SpreadLoader] Ticker must be explicitly provided. Silent fallbacks are forbidden.")
        _t_prefix = ticker.lower()
        if prefix == "spread":
            candidates = self._spread_candidates(_t_prefix)
            files = [path for _, path in candidates]
        else:
            pattern = os.path.join(self.data_dir, f"{_t_prefix}_{prefix}_*.csv")
            files = sorted(glob.glob(pattern))
        if not files:
            if prefix == "spread":
                self._last_refresh_error = "No valid formal calendar spread CSV found"
            logger.warning(f"[SpreadLoader] No CSV files found for prefix={prefix}, ticker={_t_prefix} in {self.data_dir}")
            return False

        csv_path = files[-1]
        try:
            before = self._file_fingerprint(csv_path) if prefix == "spread" else None
            df = pd.read_csv(csv_path)
            if prefix == "spread":
                df = self._validate_spread_frame(df)
                after = self._file_fingerprint(csv_path)
                if before != after:
                    raise OSError("calendar spread replaced while being read")
        except (OSError, ValueError, pd.errors.ParserError) as exc:
            if prefix == "spread":
                self._last_refresh_error = f"{type(exc).__name__}: {exc}"
            logger.warning(f"[SpreadLoader] Rejected {csv_path}: {exc}")
            return False

        if prefix != "spread":
            ts_col = next((c for c in ("ts", "timestamp", "datetime") if c in df.columns), None)
            if ts_col is None:
                logger.warning(f"[SpreadLoader] No timestamp column in {csv_path}: cols={list(df.columns)}")
                return False
            df = df.copy()
            df[ts_col] = pd.to_datetime(df[ts_col], errors="coerce")
            df = df.dropna(subset=[ts_col]).set_index(ts_col)
        df.index = df.index.tz_localize(None) if df.index.tz is not None else df.index

        if prefix == "spread":
            # Calendar spread CSV has pre-computed spread_z, vwap_z
            self._far_df = df  # store in _far_df for lookup
            self._csv_paths[prefix] = csv_path
            self._spread_fingerprint = after
            self._spread_csv_mtime = os.path.getmtime(csv_path)
            self._last_successful_refresh = datetime.now(timezone.utc).isoformat()
            self._last_refresh_error = None
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
            
        row = self._far_df.iloc[idx]
        return row

    def _fill_default_spread(self, bar: dict, ts: pd.Timestamp | None) -> None:
        """Set spread_z to a safe default (0.0 = neutral).
        
        💡 [Fixed 2026-05-27] Keep far_close at 0.0 to signal missing data.
        """
        bar["spread_z"] = 0.0
        # 2026-07-09 Hermes Agent: Default values for Spread EMA 20 and EMA 60 versions
        bar["spread_ema_20"] = 0.0
        bar["spread_ema_60"] = 0.0
        bar["near_close"] = bar.get("Close", 0.0)
        bar["far_close"] = 0.0
        bar["spread_ma"] = 0.0
        bar["spread_std"] = 0.0

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
        self._spread_fingerprint = None

    def status(self) -> dict[str, Any]:
        return {
            "csv": self._csv_paths.get("spread") or self._csv_paths.get("far"),
            "near_csv": self._csv_paths.get("near"),
            "rows": len(self._far_df) if self._far_df is not None else 0,
            "near_rows": len(self._near_df) if self._near_df is not None else 0,
            "last_spread_z": self._last_spread_data.get("spread_z"),
            "last_refresh_error": self._last_refresh_error,
            "last_successful_refresh": self._last_successful_refresh,
        }


# Singleton for use across the system
_shared_loader: SpreadLoader | None = None


def get_spread_loader() -> SpreadLoader:
    """Get or create the shared SpreadLoader singleton."""
    global _shared_loader
    if _shared_loader is None:
        _shared_loader = SpreadLoader()
    return _shared_loader
