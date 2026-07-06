#!/usr/bin/env python3
"""
Historical Data Expander — Monthly chunked downloader for long-term backtesting.
Supports extending data to multiple years while avoiding API timeouts.
Directly outputs to Parquet database.
"""
import sys
import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
from dateutil.relativedelta import relativedelta

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.shioaji_session import get_api
from core.data_sentinel import data_sentinel
from core.data_manager import data_manager

def expand_history(ticker: str, years: int):
    print(f"🚀 Starting Historical Expansion for {ticker} ({years} years)")
    
    # ... date calculation ...
    end_date = datetime.now()
    start_date = end_date - relativedelta(years=years)
    
    chunks = []
    curr = start_date
    while curr < end_date:
        next_month = curr + relativedelta(months=1)
        chunks.append((curr.strftime("%Y-%m-%d"), min(next_month, end_date).strftime("%Y-%m-%d")))
        curr = next_month

    print(f"📦 Plan: {len(chunks)} monthly chunks to download.")

    # 2. Login
    api = get_api()
    
    # Resolve contract
    if ticker == "TXFR1":
        contract = api.Contracts.Futures.TXF.TXFR1
    else:
        # Assuming stock
        contract = api.Contracts.Stocks[ticker]
    
    # 3. Load Existing
    df_master = data_manager.load_historical(ticker)
    initial_rows = len(df_master)
    
    # 4. Fetch Chunks
    for idx, (s, e) in enumerate(chunks):
        print(f"[{idx+1}/{len(chunks)}] Fetching {s} to {e}...", end=" ", flush=True)
        try:
            kbars = api.kbars(contract, start=s, end=e)
            df_chunk = pd.DataFrame({**kbars})
            if df_chunk.empty:
                print("⚠️ Empty")
                continue
            
            df_chunk.ts = pd.to_datetime(df_chunk.ts)
            df_chunk = df_chunk.set_index("ts")
            
            # Map columns
            df_chunk = df_chunk.rename(columns={
                "Open": "Open", "High": "High", "Low": "Low", "Close": "Close", "Volume": "Volume"
            })[['Open', 'High', 'Low', 'Close', 'Volume']]
            
            # Merge Safely using Sentinel and Save directly via DataManager
            df_master = data_sentinel.merge_and_clean(df_master, df_chunk)
            data_manager.save_historical(ticker, df_master) # Save each month to prevent loss on crash
            
            print(f"✅ Added {len(df_chunk)} bars (Total: {len(df_master)})")
            # Rate limiting
            time.sleep(1.5)
        except Exception as exc:
            print(f"❌ Error: {exc}")
            time.sleep(5)

    print(f"\n✨ DONE! Database final state: {len(df_master)} rows.")
    print(f"📁 Database path: {data_manager.get_path(ticker)}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Expand historical K-bar database.")
    parser.add_argument("--ticker", type=str, default="TXFR1", help="Ticker symbol (e.g. TXFR1, 2330)")
    parser.add_argument("--years", type=int, default=1, help="Number of years to go back")
    args = parser.parse_args()
    
    expand_history(args.ticker, args.years)
