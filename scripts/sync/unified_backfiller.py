#!/usr/bin/env python3
"""
Unified Backfiller — Automatically patch gaps in TMF historical data using Shioaji.
Uses core.data_sentinel to find missing periods and performs atomic merges.
"""
import sys
from pathlib import Path

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd
import shioaji as sj
from datetime import datetime, timedelta
import yaml
import time
from core.data_sentinel import data_sentinel
from core.shioaji_session import get_api

# Configuration
DATA_FILE = ROOT / "data" / "tmf_full_2026.csv"

def backfill_tmf():
    print(f"🔍 Auditing data integrity: {DATA_FILE}")
    
    if not DATA_FILE.exists():
        print(f"❌ Data file not found: {DATA_FILE}")
        return

    # 1. Load existing data
    df = pd.read_csv(DATA_FILE, parse_dates=["timestamp"], index_col="timestamp")
    df = df.sort_index()
    # Ensure index name is 'timestamp'
    df.index.name = "timestamp"
    
    # 2. Audit gaps
    gaps = data_sentinel.audit_gaps(df)
    
    if not gaps:
        print("✅ No gaps detected. Data is complete.")
        return

    print(f"🚨 Detected {len(gaps)} gaps. Starting backfill...")
    for start, end in gaps:
        print(f"  • Range: {start} to {end}")

    # 3. Login to Shioaji
    api = get_api()

    # 4. Fetch and Merge
    contract = api.Contracts.Futures.TXF.TXFR1  # Main TMF contract
    new_data_count = 0
    
    for start_gap, end_gap in gaps:
        try:
            # Shift end slightly to ensure we catch the boundary bar
            fetch_end = (end_gap + timedelta(minutes=5)).strftime("%Y-%m-%d")
            fetch_start = start_gap.strftime("%Y-%m-%d")
            
            print(f"📥 Fetching {fetch_start} to {fetch_end}...")
            kbars = api.kbars(contract, start=fetch_start, end=fetch_end)
            df_new = pd.DataFrame({**kbars})
            df_new.ts = pd.to_datetime(df_new.ts)
            df_new = df_new.set_index("ts")
            
            # Map Shioaji columns to our standard format
            df_new = df_new.rename(columns={
                "Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume"
            })[['Open', 'High', 'Low', 'Close', 'Volume']]
            
            # Ensure index name is 'timestamp' for consistency
            df_new.index.name = "timestamp"
            
            # Use Sentinel to merge safely
            df = data_sentinel.merge_and_clean(df, df_new)
            new_data_count += len(df_new)
            
            # Rate limiting safety
            time.sleep(1)
        except Exception as e:
            print(f"⚠️ Error fetching gap {start_gap}: {e}")

    # 5. Save back to CSV
    if new_data_count > 0:
        df.to_csv(DATA_FILE, index=True)
        print(f"✅ Backfill complete. Added {new_data_count} bars to {DATA_FILE}")
    else:
        print("ℹ️ No new data added (possibly out of range or market closed).")

if __name__ == "__main__":
    backfill_tmf()
