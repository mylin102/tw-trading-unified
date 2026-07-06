#!/usr/bin/env python3
"""
Backfill missing night session kbars from Shioaji API.
Patches today's CSV with all 5-min bars since night session open (15:00).
"""
import sys
sys.path.insert(0, '.')

import pandas as pd
import numpy as np
from datetime import datetime, time as dtime
from pathlib import Path

def backfill_night_session():
    # Login to Shioaji
    try:
        import login.shioaji_login as shioaji_login
        api = shioaji_login.login()
        if not api:
            print("❌ Shioaji login failed")
            return
    except Exception as e:
        print(f"❌ API login error: {e}")
        return

    try:
        # Get TMF contract
        contract = api.Contracts.Futures.TMF.TMFR1
        if not contract:
            # Try to find any TMF contract
            tmf_list = list(api.Contracts.Futures.TMF)
            if tmf_list:
                contract = sorted(tmf_list, key=lambda c: c.code)[0]
                print(f"⚠️ TMFR1 not found, using {contract.code}")
            else:
                print("❌ No TMF contracts found")
                return

        today = datetime.now().date()
        today_str = today.strftime('%Y-%m-%d')
        
        print(f"📊 Fetching night session kbars for {today_str}")
        print(f"   Contract: {contract.code}")

        # Fetch kbars from today
        kbars = api.kbars(contract, start=today_str, end=today_str)
        df = pd.DataFrame({**kbars})
        
        if df.empty:
            print("⚠️ No kbars returned from API for today")
            print("   This may be normal if night session just started or data delayed")
            return

        df.ts = pd.to_datetime(df.ts)
        df.set_index('ts', inplace=True)
        
        # Resample to 5-min
        df_5m = df.resample('5min', label='right', closed='left').agg({
            'Open': 'first',
            'High': 'max',
            'Low': 'min',
            'Close': 'last',
            'Volume': 'sum',
        }).dropna(subset=['Open', 'High', 'Low', 'Close'])

        print(f"✅ Got {len(df_5m)} bars from API")
        print(f"   Range: {df_5m.index[0]} → {df_5m.index[-1]}")

        # Filter to night session only (15:00+)
        night_bars = df_5m[df_5m.index.hour >= 15]
        
        if night_bars.empty:
            print("⚠️ No night session bars found (15:00+)")
            print(f"   Available hours: {df_5m.index.hour.unique()}")
            return

        print(f"🌙 Night session bars: {len(night_bars)}")

        # Add trading day (night session belongs to NEXT trading day)
        from core.date_utils import get_trading_day
        night_bars = night_bars.copy()
        night_bars['trading_day'] = get_trading_day(night_bars.index)

        # Prepare CSV columns matching existing format
        night_bars['timestamp'] = night_bars.index.strftime('%Y-%m-%d %H:%M:%S')
        night_bars['session'] = 2  # Night session
        night_bars['score'] = 0.0
        night_bars['regime'] = 'NORMAL'
        night_bars['amount'] = 0
        night_bars['bull_align'] = False
        night_bars['bear_align'] = False
        night_bars['in_pb_zone'] = False

        # Read existing CSV
        csv_path = Path(f'logs/market_data/TMF_{today.strftime("%Y%m%d")}_PAPER_indicators.csv')
        
        if csv_path.exists():
            existing = pd.read_csv(csv_path)
            # Merge: existing + new night bars, deduplicate by timestamp
            combined = pd.concat([existing, night_bars], ignore_index=True)
            combined = combined.drop_duplicates(subset=['timestamp'], keep='last')
            combined = combined.sort_values('timestamp').reset_index(drop=True)
        else:
            combined = night_bars.reset_index(drop=True)

        # Select columns in order matching existing format
        target_cols = ['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume', 
                       'trading_day', 'session', 'score', 'regime', 
                       'open', 'high', 'low', 'close', 'volume', 'amount',
                       'bull_align', 'bear_align', 'in_pb_zone']
        
        # Ensure all columns exist
        for col in target_cols:
            if col not in combined.columns:
                combined[col] = np.nan

        combined = combined[target_cols]
        combined.to_csv(csv_path, index=False)

        print(f"✅ Backfilled {len(night_bars)} night bars to {csv_path}")
        print(f"   Total bars in file: {len(combined)}")
        print(f"   Latest: {combined['timestamp'].iloc[-1]}")

    except Exception as e:
        import traceback
        print(f"❌ Backfill error: {e}")
        traceback.print_exc()
    finally:
        try:
            from shioaji import Shioaji
            api.logout()
        except:
            pass

if __name__ == '__main__':
    backfill_night_session()
