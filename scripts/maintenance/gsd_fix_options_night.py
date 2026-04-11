#!/usr/bin/env python3
import pandas as pd
import numpy as np
from pathlib import Path
import datetime
import os
import sys

# Add project root to path
sys.path.append(os.getcwd())

from core.date_utils import get_session

def fix_options_night():
    tmf_path = Path("logs/market_data/TMF_20260410_PAPER_indicators.csv")
    opt_path = Path("strategies/options/logs/paper_trading/OPTIONS_20260410_indicators.csv")
    
    if not tmf_path.exists():
        print(f"❌ TMF source file not found: {tmf_path}")
        return

    print(f"🔍 Reading TMF data from {tmf_path}...")
    tmf = pd.read_csv(tmf_path)
    if tmf.empty:
        print("❌ TMF data is empty")
        return

    # Basic mapping: TMF columns to Options columns
    # timestamp,session,score,side,price_mtx,strike,dte,mid_trend,iv,delta,gamma,vega,vwap,sqz_on
    
    rows = []
    for _, r in tmf.iterrows():
        ts = r["timestamp"]
        price = float(r["close"])
        
        if price <= 0: continue
        
        # Mock values for Greeks/Indicators to fill the gap
        # Strike: round to 100
        strike = round(price / 100) * 100
        
        row = {
            "timestamp": ts,
            "session": int(r.get("session", 2)),
            "score": float(r.get("score", 0.0)),
            "side": "",
            "price_mtx": price,
            "strike": strike,
            "dte": 5.0, # Approximate
            "mid_trend": str(r.get("regime", "NORMAL")),
            "iv": 0.25,
            "delta": 0.5,
            "gamma": 0.0001,
            "vega": 10.0,
            "vwap": price,
            "sqz_on": bool(r.get("sqz_on", False))
        }
        rows.append(row)

    df_opt = pd.DataFrame(rows)
    
    # Save to file (overwrite to ensure clean state)
    df_opt.to_csv(opt_path, index=False)
    print(f"✅ Successfully patched {len(df_opt)} rows to {opt_path}")

if __name__ == "__main__":
    fix_options_night()
