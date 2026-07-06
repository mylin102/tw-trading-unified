#!/usr/bin/env python3
"""
Fetch near-month and far-month MXF futures data using real account API.

This script uses shioaji_session.get_api() to login and fetch kbars data
for both near-month and far-month contracts, then saves them as CSV files
for use by calendar_condor_v2 strategy.

Usage:
    python scripts/fetch_far_month_data.py [--days 7] [--interval 1]
"""

import os
import sys
import argparse
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.shioaji_session import get_api
from core.broker.shioaji_compat import kbars_to_dataframe


def get_contracts(api, category="MXF"):
    """Get near-month and far-month contracts."""
    contracts = list(api.Contracts.Futures[category])
    
    # Filter out rolling contracts (R1, R2, etc.)
    regular = [c for c in contracts if not c.code.endswith(('R1', 'R2', 'R3'))]
    
    # Sort by delivery date
    sorted_c = sorted(regular, key=lambda c: c.delivery_date)
    
    if len(sorted_c) < 2:
        print(f"ERROR: Not enough {category} contracts: {len(sorted_c)}")
        for c in sorted_c:
            print(f"  {c.code} delivery={c.delivery_date}")
        return None, None
    
    near = sorted_c[0]
    far = sorted_c[1]
    
    print(f"Near contract: {near.code} (delivery: {near.delivery_date})")
    print(f"Far contract:  {far.code} (delivery: {far.delivery_date})")
    
    return near, far


def fetch_kbars(api, contract, days=7):
    """Fetch kbars data for a contract."""
    end = datetime.now()
    start = end - timedelta(days=days)
    start_str = start.strftime("%Y-%m-%d")
    end_str = end.strftime("%Y-%m-%d")
    
    print(f"Fetching {contract.code}: {start_str} ~ {end_str} ...", end=" ", flush=True)
    
    try:
        kbars = api.kbars(contract=contract, start=start_str, end=end_str)
        
        # 轉換為DataFrame (使用兼容性助手)
        df = kbars_to_dataframe(kbars)
        
        if df.empty:
            print("NO DATA (empty ts)")
            return None
        
        # 將索引 ts 轉換為列
        df = df.reset_index()
        print(f"OK ({len(df)} bars)")
        return df
    
    except Exception as e:
        print(f"ERROR: {e}")
        return None


def main():
    parser = argparse.ArgumentParser(description="Fetch far-month futures data")
    parser.add_argument("--days", type=int, default=7, help="Days of history to fetch")
    parser.add_argument("--interval", type=str, default="1", help="Kbar interval in minutes")
    args = parser.parse_args()
    
    print("=== Fetch Far-Month MXF Data ===")
    print(f"Period: {args.days} days")
    print()
    
    # Get real account API (this will login with real credentials)
    print("Logging in with real account...")
    try:
        api = get_api()
        print(f"Logged in successfully")
    except Exception as e:
        print(f"ERROR: Login failed: {e}")
        return 1
    
    print()
    
    # Get contracts
    near, far = get_contracts(api, "MXF")
    if not near or not far:
        return 1
    
    print()
    
    # Fetch data
    df_near = fetch_kbars(api, near, days=args.days)
    if df_near is None:
        print("ERROR: Failed to fetch near-month data")
        return 1
    
    df_far = fetch_kbars(api, far, days=args.days)
    if df_far is None:
        print("ERROR: Failed to fetch far-month data")
        return 1
    
    print()
    
    # Save to CSV
    output_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    os.makedirs(output_dir, exist_ok=True)
    
    today_str = datetime.now().strftime("%Y%m%d")
    
    near_path = os.path.join(output_dir, f"mxf_near_{near.code}_{today_str}.csv")
    far_path = os.path.join(output_dir, f"mxf_far_{far.code}_{today_str}.csv")
    
    df_near.to_csv(near_path, index=False)
    df_far.to_csv(far_path, index=False)
    
    print(f"Saved near: {near_path} ({len(df_near)} rows)")
    print(f"Saved far:  {far_path} ({len(df_far)} rows)")
    
    # Show merge stats
    df_near['ts'] = pd.to_datetime(df_near['ts'])
    df_far['ts'] = pd.to_datetime(df_far['ts'])
    merged = pd.merge(df_near[['ts', 'Close']], df_far[['ts', 'Close']],
                      on='ts', suffixes=('_near', '_far'))
    print(f"\nOverlapping timestamps: {len(merged)}")
    if not merged.empty:
        print(f"Near range: {df_near['ts'].min()} ~ {df_near['ts'].max()}")
        print(f"Far range:  {df_far['ts'].min()} ~ {df_far['ts'].max()}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
ain())
