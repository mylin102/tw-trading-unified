#!/usr/bin/env python3
"""
Maintenance: Market Data Sync & Clean (Taifex Standard)
1. Fetch missing K-bars from Shioaji.
2. Group by Trading_Day (Night + Day session).
3. Resample 1-min gaps with ffill to ensure data integrity.
4. Add Session ID (1=Day, 2=Night).
"""
import sys
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta
import os

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
sys.path.append(str(ROOT))

from core.shioaji_session import get_api, logout
from core.date_utils import get_trading_day, get_session, fetch_holidays

def sync_ticker_data(api, ticker, days_back=5):
    """Sync and resample data for a specific ticker."""
    print(f"\n[sync] Processing {ticker} (Last {days_back} days)...")
    
    # 1. Fetch holidays first
    holidays = fetch_holidays(api)
    
    # 2. Get contract
    try:
        if "." in ticker: # Stock
            contract = api.Contracts.Stocks[ticker.split(".")[0]]
        else: # Futures (default TXF near month)
            contract = getattr(api.Contracts.Futures.TXF, f"TXF{datetime.now().strftime('%Y%m')}")
    except Exception as e:
        print(f"  [!] Contract lookup failed for {ticker}: {e}")
        return

    # 3. Download K-bars
    end_date = datetime.now().strftime("%Y-%m-%d")
    start_date = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    
    kb = api.kbars(contract, start=start_date, end=end_date)
    df = pd.DataFrame({**kb})
    if df.empty:
        print("  [!] No data returned from API.")
        return

    df['ts'] = pd.to_datetime(df['ts'])
    
    # 4. Apply Taifex Logic
    df['Trading_Day'] = get_trading_day(df['ts'], holidays=holidays)
    df['Session'] = df['ts'].apply(get_session)
    
    # 5. Group by Trading Day and Process
    data_dir = ROOT / "data" / "taifex_raw"
    data_dir.mkdir(parents=True, exist_ok=True)
    
    for t_day, group in df.groupby('Trading_Day'):
        t_day_str = t_day.strftime("%Y%m%d")
        file_path = data_dir / f"{ticker}_{t_day_str}.csv"
        
        # ── Data Integration Logic ──
        # 1. Standardize columns
        group = group.rename(columns={
            "Open": "open", "High": "high", "Low": "low", "Close": "close", "Volume": "volume"
        })
        group = group.set_index('ts').sort_index()

        # 2. Merge with existing data to avoid gaps in incremental sync
        if file_path.exists():
            try:
                existing = pd.read_csv(file_path, index_col='ts', parse_dates=True)
                # Combine and prioritize new data on duplicates (GSD: keep='last')
                group = pd.concat([existing, group])
                # Ensure index is datetime and drop duplicates based on timestamp
                group.index = pd.to_datetime(group.index)
                group = group[~group.index.duplicated(keep='last')].sort_index()
            except Exception as e:
                print(f"  [!] Error merging existing file {file_path.name}: {e}")

        # 3. Resample to 1min to fill gaps (Crucial for illiquid night sessions)
        resampled = group.resample('1min', closed='left', label='left').agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 
            'volume': 'sum', 'Trading_Day': 'first', 'Session': 'first'
        })
        
        # 4. Fill missing price values (Vacuum filling)
        resampled['close'] = resampled['close'].ffill()
        resampled['open'] = resampled['open'].fillna(resampled['close'])
        resampled['high'] = resampled['high'].fillna(resampled['close'])
        resampled['low'] = resampled['low'].fillna(resampled['close'])
        resampled['volume'] = resampled['volume'].fillna(0)
        resampled['Trading_Day'] = resampled['Trading_Day'].ffill().bfill()
        resampled['Session'] = resampled['Session'].ffill().bfill()
        
        # 5. Save (Clean Overwrite)
        resampled.to_csv(file_path)
        print(f"  [✓] Updated: {file_path.name} ({len(resampled)} rows)")

def main():
    api = get_api()
    # Default sync: TXF (Futures) and some key stocks if needed
    tickers = ["TXF"] 
    # Optional: Read watchlist from config
    import yaml
    try:
        with open(ROOT / "config" / "stocks.yaml", "r") as f:
            cfg = yaml.safe_load(f)
            tickers += cfg.get("stocks", {}).get("watchlist", [])[:3] # Limit to top 3 for maintenance
    except:
        pass

    for t in tickers:
        try:
            sync_ticker_data(api, t)
        except Exception as e:
            print(f"  [✗] Error syncing {t}: {e}")
    
    print("\n[maintenance] Data sync complete.")

if __name__ == "__main__":
    main()
