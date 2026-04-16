#!/usr/bin/env python3
"""
補抓今天的 K bar 資料到 data/tmf_full_2026.csv
用法: python3 scripts/patch_today_kbars.py
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

import pandas as pd
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze


def download_futures_today(api, csv_path):
    """Download today's 1-min kbars for TMF and append to tmf_full_2026.csv."""
    today = datetime.now().strftime("%Y-%m-%d")
    print(f"Fetching TMF kbars for {today}...")

    # Find contract
    contracts = list(api.Contracts.Futures["TMF"])
    # Try D6 (April 2026) first, then R1 (weekly), then first available
    target = next((c for c in contracts if "D6" in str(c.code) or "R1" in str(c.code)), contracts[0] if contracts else None)
    if target is None:
        print("No TMF contracts found")
        return
    print(f"Contract: {target.code}")

    kbars = api.kbars(target, start=today, end=today)
    if kbars is None or (hasattr(kbars, 'ts') and len(kbars.ts) == 0):
        print("No data returned. Market may be closed.")
        return

    df = pd.DataFrame({"timestamp": kbars.ts, "Open": kbars.Open, "High": kbars.High,
                       "Low": kbars.Low, "Close": kbars.Close, "Volume": kbars.Volume,
                       "Amount": kbars.Amount})
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    print(f"Downloaded {len(df)} bars")

    # Load existing data
    if csv_path.exists():
        existing = pd.read_csv(csv_path, parse_dates=["timestamp"])
        # Remove today's existing rows (if any) and append new
        existing = existing[existing["timestamp"].dt.date < pd.Timestamp(today).date()]
        combined = pd.concat([existing, df], ignore_index=True)
    else:
        combined = df

    # Calculate indicators
    print("Calculating indicators...")
    ind_df = combined.set_index("timestamp").sort_index()
    result = calculate_futures_squeeze(ind_df)

    # Save
    result.index.name = "timestamp"
    result = result.reset_index()
    result.to_csv(csv_path, index=False)
    print(f"Saved {len(result)} rows to {csv_path}")
    print(f"Latest bar: {result['timestamp'].iloc[-1]}")


def download_stocks_today(api):
    """Update 5-min kbars for all stock watchlist tickers."""
    from strategies.stocks.monitor import StockMonitor  # For config
    cfg = yaml.safe_load(open(ROOT / "config" / "stocks.yaml"))
    watchlist = cfg.get("stocks", {}).get("watchlist", [])

    today = datetime.now().strftime("%Y-%m-%d")
    print(f"\nUpdating stocks for {today}...")

    for ticker in watchlist:
        try:
            contract = api.Contracts.Stocks[ticker]
            kbars = api.kbars(contract, start=today, end=today)
            if kbars is None or (hasattr(kbars, 'ts') and len(kbars.ts) == 0):
                print(f"  {ticker}: no data")
                continue

            new_df = pd.DataFrame({"Date": kbars.ts, "Open": kbars.Open, "High": kbars.High,
                                   "Low": kbars.Low, "Close": kbars.Close, "Volume": kbars.Volume,
                                   "Amount": kbars.Amount})
            new_df["Date"] = pd.to_datetime(new_df["Date"])

            csv_path = ROOT / "data" / "taifex_raw" / f"STOCK_{ticker}_5m.csv"
            if csv_path.exists():
                existing = pd.read_csv(csv_path)
                if "Date" not in existing.columns and "timestamp" in existing.columns:
                    existing = existing.rename(columns={"timestamp": "Date"})
                existing["Date"] = pd.to_datetime(existing["Date"])
                # Remove today rows and append
                today_ts = pd.Timestamp(today)
                existing = existing[existing["Date"].dt.date < today_ts.date()]
                combined = pd.concat([existing, new_df], ignore_index=True)
            else:
                combined = new_df

            combined.to_csv(csv_path, index=False)
            print(f"  {ticker}: {len(combined)} rows total ({len(new_df)} new)")
        except Exception as e:
            print(f"  {ticker}: ERROR {e}")


def main():
    load_dotenv(override=True)
    user_id = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_PERSON_ID")
    password = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_PASSWD")

    if not user_id or not password:
        print("Missing credentials. Using existing session...")
        from core.shioaji_session import get_api
        api = get_api()
    else:
        import shioaji as sj
        api = sj.Shioaji()
        api.login(user_id, password, contracts_timeout=10000)
        print("Logged in")

    # Futures
    csv_path = ROOT / "data" / "tmf_full_2026.csv"
    download_futures_today(api, csv_path)

    # Stocks
    download_stocks_today(api)

    if hasattr(api, 'logout'):
        api.logout()
    print("\n✅ Done")


if __name__ == "__main__":
    main()
