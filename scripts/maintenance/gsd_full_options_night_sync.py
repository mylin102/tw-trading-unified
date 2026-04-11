#!/usr/bin/env python3
import pandas as pd
import numpy as np
from pathlib import Path
import datetime
import os
import sys

# Add project root to path
sys.path.append(os.getcwd())

def full_options_night_sync():
    # Source: Current paper indicators which contains virtual/recent bars
    source_path = Path("logs/market_data/TMF_20260410_PAPER_indicators.csv")
    dest_path = Path("strategies/options/logs/paper_trading/OPTIONS_20260410_indicators.csv")
    
    if not source_path.exists():
        print(f"❌ Source file not found: {source_path}")
        return

    print(f"🔍 Reading live TMF indicators from {source_path}...")
    hist = pd.read_csv(source_path)
    hist["timestamp"] = pd.to_datetime(hist["timestamp"])
    
    # Use all rows from this file as it's already 4/10 (Night session)
    night_data = hist.copy().sort_values("timestamp")
    
    if night_data.empty:
        print("❌ No night session data found in source")
        return

    print(f"✅ Found {len(night_data)} bars for night session.")

    # Load existing options file if any
    existing_df = None
    if dest_path.exists():
        try:
            existing_df = pd.read_csv(dest_path)
            existing_df["timestamp"] = pd.to_datetime(existing_df["timestamp"])
        except:
            pass

    rows = []
    for _, r in night_data.iterrows():
        ts_str = r["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
        price = float(r["Close"])
        
        # Calculate strike
        strike = round(price / 100) * 100
        
        row = {
            "timestamp": ts_str,
            "session": 2,
            "score": 0.0,
            "side": "",
            "price_mtx": price,
            "strike": strike,
            "dte": 5.5,
            "mid_trend": "NORMAL",
            "iv": 0.25,
            "delta": 0.5,
            "gamma": 0.0001,
            "vega": 10.0,
            "vwap": price,
            "sqz_on": False
        }
        rows.append(row)

    new_df = pd.DataFrame(rows)
    new_df["timestamp"] = pd.to_datetime(new_df["timestamp"])

    if existing_df is not None:
        # Merge and dedup
        combined = pd.concat([new_df, existing_df]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
    else:
        combined = new_df

    combined.to_csv(dest_path, index=False)
    print(f"🚀 Successfully synced {len(combined)} total night bars to {dest_path}")

if __name__ == "__main__":
    full_options_night_sync()
