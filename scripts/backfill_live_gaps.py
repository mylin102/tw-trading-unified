#!/usr/bin/env python3
"""
Backfill Live Gaps — Fill missing 5-min bars in today's indicator CSV using Shioaji API.
Detects gaps > 15 min, fetches raw OHLCV, merges atomically.
Usage:
    python3 scripts/backfill_live_gaps.py [--write] [--date YYYYMMDD]

    --write     Actually write merged data (dry-run by default)
    --date      Backfill specific date (default: today's session date)
"""

import sys
from pathlib import Path
ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
from datetime import datetime, timedelta
import shioaji as sj
import time
from core.shioaji_session import get_api
from core.date_utils import get_session_date_str

BAR_INTERVAL = 5  # minutes
MAX_GAP_MINUTES = 15
OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]

def find_gaps(df):
    """Find gaps > MAX_GAP_MINUTES in the indicator CSV index."""
    ts = pd.to_datetime(df["timestamp"])
    diffs = ts.diff()
    gaps = []
    for i in range(1, len(ts)):
        gap_min = diffs.iloc[i].total_seconds() / 60.0
        if gap_min > MAX_GAP_MINUTES:
            gaps.append((ts.iloc[i - 1], ts.iloc[i], gap_min))
    return gaps


def fetch_raw_bars(api, contract, start, end):
    """Fetch raw 1-min bars from Shioaji, aggregate to 5-min OHLCV."""
    kbars = api.kbars(contract, start=start.strftime("%Y-%m-%d"),
                      end=end.strftime("%Y-%m-%d"))
    if kbars is None or len(kbars.ts) == 0:
        return pd.DataFrame()

    df = pd.DataFrame({k: v for k, v in dict(kbars).items()})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.set_index("ts").sort_index()

    # Aggregate 1-min → 5-min
    ohlc = df["Close"].resample(f"{BAR_INTERVAL}min").agg({
        "Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"
    })
    # Flatten multi-index columns from agg dict
    ohlc.columns = ["Open", "High", "Low", "Close", "Volume"]
    ohlc.index.name = "ts"
    return ohlc


def backfill_indicator_csv(indicator_path, api=None, dry_run=True):
    """Backfill gaps in today's indicator CSV using Shioaji API."""
    indicator_path = Path(indicator_path)
    if not indicator_path.exists():
        print(f"❌ Indicator CSV not found: {indicator_path}")
        return False, "FILE_NOT_FOUND"

    # 1. Read indicator CSV
    df = pd.read_csv(indicator_path)
    if "timestamp" not in df.columns:
        print(f"❌ No timestamp column in {indicator_path}")
        return False, "NO_TIMESTAMP_COL"

    # 2. Find gaps
    gaps = find_gaps(df)
    if not gaps:
        print("✅ No gaps found. Data is complete.")
        return True, "NO_GAPS"

    print(f"🔍 Found {len(gaps)} gaps in {indicator_path.name}:")
    for s, e, m in gaps:
        print(f"    {s} → {e}  ({m:.0f} min)")

    # 3. Login if needed
    if api is None:
        api = get_api()

    # 4. Contract
    contract = api.Contracts.Futures.TXF.TXFR1

    # 5. For each gap, fetch and merge
    new_bars = []
    for gap_start, gap_end, _ in gaps:
        try:
            fetch_start = gap_start - timedelta(minutes=5)  # overlap for safety
            fetch_end = gap_end + timedelta(minutes=5)

            print(f"  📥 Fetching {fetch_start} → {fetch_end}...", end=" ")
            ohlc = fetch_raw_bars(api, contract, fetch_start, fetch_end)
            if ohlc.empty:
                print("no data")
                continue

            # Filter to only the gap range
            mask = (ohlc.index > gap_start) & (ohlc.index < gap_end)
            gap_bars = ohlc[mask].copy()
            if gap_bars.empty:
                print("no bars in gap range")
                continue

            print(f"{len(gap_bars)} bars")
            new_bars.append(gap_bars)
            time.sleep(0.5)  # rate limit
        except Exception as e:
            print(f"⚠️  Error: {e}")

    if not new_bars:
        print("ℹ️  No new bars to merge.")
        return True, "NO_NEW_BARS"

    # 6. Merge
    df_new = pd.concat(new_bars).sort_index()
    df_new.index.name = "timestamp"
    df_new = df_new.reset_index()

    # Parse existing timestamps
    df["timestamp"] = pd.to_datetime(df["timestamp"])

    if dry_run:
        print(f"\n📋 DRY RUN — would merge {len(df_new)} bars:")
        for _, row in df_new.iterrows():
            print(f"    {row['timestamp']}  O={row['Open']:.0f} H={row['High']:.0f} "
                  f"L={row['Low']:.0f} C={row['Close']:.0f} V={row['Volume']:.0f}")
        print(f"\n✅ Dry run complete. Pass --write to apply.")
        return True, f"DRY_RUN:{len(df_new)}_BARS"

    # 7. Write merge
    # Add only OHLCV columns; other columns will be NaN (recomputed by monitor)
    for col in OHLCV_COLS:
        df_new[col] = df_new[col].astype(float)
    df_new["Volume"] = df_new["Volume"].astype(float)

    df_combined = pd.concat([df, df_new], ignore_index=True)
    df_combined = df_combined.drop_duplicates(subset=["timestamp"], keep="first")
    df_combined = df_combined.sort_values("timestamp")
    df_combined.to_csv(indicator_path, index=False)

    print(f"✅ Merged {len(df_new)} bars into {indicator_path.name}")
    print(f"   Total rows: {len(df)} → {len(df_combined)}")
    return True, f"MERGED:{len(df_new)}_BARS"


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backfill live gaps in indicator CSV")
    parser.add_argument("--write", action="store_true", help="Actually write merged data")
    parser.add_argument("--date", type=str, default=None,
                        help="Session date YYYYMMDD (default: today)")
    args = parser.parse_args()

    today = args.date or datetime.now().strftime("%Y%m%d")
    session_date = get_session_date_str(datetime.now())

    # Find the indicator CSV
    log_dir = ROOT / "logs" / "market_data"
    candidates = sorted(log_dir.glob(f"TMF_{session_date}*_indicators.csv"))
    if not candidates:
        candidates = sorted(log_dir.glob(f"TMF_{today}*_indicators.csv"))

    if not candidates:
        print(f"❌ No indicator CSV found for date {session_date}/{today}")
        sys.exit(1)

    path = candidates[-1]  # most recent
    print(f"📄 Target: {path}")

    success, msg = backfill_indicator_csv(path, dry_run=not args.write)
    print(f"\n{'✅' if 'MERGED' in msg or 'NO_GAPS' in msg or 'DRY_RUN' in msg else '❌'} {msg}")

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
