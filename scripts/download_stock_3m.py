"""
Phase 1.1: Download 3 months of 5-minute kbars for all stock watchlist tickers.
Usage: python3 scripts/download_stock_3m.py
"""
import os
import sys
from pathlib import Path
from datetime import datetime, timedelta

import yaml
import pandas as pd
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "data" / "taifex_raw"
DATA_DIR.mkdir(parents=True, exist_ok=True)

def download_all(api, tickers, months=3):
    """Download 3 months of 5m kbars for each ticker."""
    start_date = (datetime.now() - timedelta(days=months * 30)).strftime("%Y-%m-%d")
    print(f"📥 Downloading from {start_date} for {len(tickers)} tickers...")

    for i, ticker in enumerate(tickers, 1):
        try:
            contract = api.Contracts.Stocks[ticker]
            if not contract:
                print(f"  [{i}/{len(tickers)}] {ticker}: Contract not found")
                continue

            kbars = api.kbars(contract, start=start_date)
            if not kbars or len(kbars.ts) == 0:
                print(f"  [{i}/{len(tickers)}] {ticker}: No data returned")
                continue

            df = pd.DataFrame({"timestamp": kbars.ts, "Open": kbars.Open, "High": kbars.High,
                               "Low": kbars.Low, "Close": kbars.Close, "Volume": kbars.Volume})
            df["timestamp"] = pd.to_datetime(df["timestamp"])
            df = df.sort_values("timestamp").reset_index(drop=True)

            # Standardize column names
            col_map = {}
            for c in df.columns:
                cl = c.lower()
                if cl in ("open", "high", "low", "close", "volume"):
                    col_map[c] = c.capitalize()
            df = df.rename(columns=col_map)

            # Merge with existing data
            file_path = DATA_DIR / f"STOCK_{ticker}_5m.csv"
            if file_path.exists():
                try:
                    existing = pd.read_csv(file_path)
                    if "timestamp" not in existing.columns and "ts" in existing.columns:
                        existing = existing.rename(columns={"ts": "timestamp"})
                    existing["timestamp"] = pd.to_datetime(existing["timestamp"], errors="coerce")
                    df = pd.concat([existing, df], ignore_index=True)
                    df = df.drop_duplicates(subset=["timestamp"]).sort_values("timestamp").reset_index(drop=True)
                except Exception as e:
                    print(f"  ⚠️ Merge error for {ticker}: {e}")

            df.to_csv(file_path, index=False)
            days_span = (df["timestamp"].max() - df["timestamp"].min()).days
            print(f"  [{i}/{len(tickers)}] {ticker}: {len(df)} rows ({days_span} days) ✓")

        except Exception as e:
            print(f"  [{i}/{len(tickers)}] {ticker}: FAILED — {e}")


def main():
    # Load watchlist
    cfg_path = ROOT / "config" / "stocks.yaml"
    cfg = yaml.safe_load(open(cfg_path))
    watchlist = cfg.get("stocks", {}).get("watchlist", [])
    print(f"Watchlist: {watchlist}")

    # Try to use existing session from main.py if available, otherwise login
    load_dotenv(override=True)
    user_id = os.getenv("SHIOAJI_API_KEY") or os.getenv("SHIOAJI_PERSON_ID")
    password = os.getenv("SHIOAJI_SECRET_KEY") or os.getenv("SHIOAJI_PASSWD")

    if not user_id or not password:
        print("❌ Missing SHIOAJI_API_KEY/SHIOAJI_PERSON_ID or SHIOAJI_SECRET_KEY/SHIOAJI_PASSWD in .env")
        sys.exit(1)

    import shioaji as sj
    api = sj.Shioaji()
    api.login(user_id, password, contracts_timeout=10000)
    print("✅ Logged in")

    download_all(api, watchlist, months=3)

    api.logout()
    print("✅ Done")


if __name__ == "__main__":
    main()
