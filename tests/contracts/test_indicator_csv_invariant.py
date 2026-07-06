"""Contract test: indicator CSV is an enriched-output artifact.

Invariant:
  Indicator CSV (MXF_*_PAPER_indicators.csv) may only be created by
  _save_bar or an enriched backfill writer that runs indicator calculations.
  Raw API backfill (_backfill_night_gaps) must NOT write to this file.

Violation symptoms:
  - Header has Close before timestamp (sorted() vs canonical order)
  - atr, vwap, sqz_on, momentum columns are all NaN on recent rows
  - File size grows but indicators stay empty

Run:  pytest tests/contracts/test_indicator_csv_invariant.py -v
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

LOGS_DIR = Path("logs/market_data")
TAG = "PAPER"


def _find_indicator_csvs(ticker: str) -> list[Path]:
    return sorted(LOGS_DIR.glob(f"{ticker}_*_{TAG}_indicators.csv"))


@pytest.fixture
def indicator_csvs(configured_ticker) -> list[Path]:
    return _find_indicator_csvs(configured_ticker)


def test_indicator_csv_header_starts_with_timestamp(indicator_csvs: list[Path]):
    """First column MUST be 'timestamp', NOT 'Close'."""
    for csv_path in indicator_csvs:
        df = pd.read_csv(csv_path, nrows=0)
        first_col = df.columns[0]
        assert first_col == "timestamp", (
            f"Header column order broken in {csv_path.name}: "
            f"first column is '{first_col}', expected 'timestamp'."
        )


def test_indicator_csv_has_enriched_data(indicator_csvs: list[Path]):
    """Last 3 rows MUST have indicators (atr, vwap, momentum, sqz_on)."""
    for csv_path in indicator_csvs:
        df = pd.read_csv(csv_path)
        if len(df) < 3:
            pytest.skip(f"{csv_path.name} too short ({len(df)} rows)")

        tail = df.tail(3)
        required_cols = ["atr", "vwap", "momentum", "sqz_on"]
        missing = [c for c in required_cols if c not in df.columns]
        if missing:
            pytest.skip(f"{csv_path.name} missing columns: {missing}")

        for col in required_cols:
            non_null = tail[col].notna().sum()
            assert non_null > 0, (
                f"Column '{col}' has {non_null}/3 non-null in tail rows of "
                f"{csv_path.name}. Raw backfill likely overwrote enriched rows."
            )


def test_indicator_csv_timestamp_parseable(indicator_csvs: list[Path]):
    """timestamp column must contain valid datetimes, not Close values."""
    for csv_path in indicator_csvs:
        df = pd.read_csv(csv_path)
        if "timestamp" not in df.columns:
            pytest.skip(f"{csv_path.name} has no timestamp column")

        ts = pd.to_datetime(df["timestamp"], errors="coerce")
        null_count = ts.isna().sum()
        total = len(df)
        ratio = null_count / max(total, 1)

        assert ratio < 0.5, (
            f"{null_count}/{total} timestamps are NaT in {csv_path.name} "
            f"(ratio={ratio:.1%}). Column misalignment."
        )


def test_no_raw_csv_contamination():
    """No raw API data files with indicator CSV naming convention."""
    suspicious = list(LOGS_DIR.glob("*_raw_*_indicators.csv"))
    suspicious += list(LOGS_DIR.glob("*_api_*_indicators.csv"))
    assert len(suspicious) == 0, (
        f"Found {len(suspicious)} suspicious files: "
        + ", ".join(s.name for s in suspicious)
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
