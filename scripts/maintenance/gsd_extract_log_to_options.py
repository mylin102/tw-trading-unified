#!/usr/bin/env python3
import pandas as pd
import datetime
import os
import re
from pathlib import Path

def extract_log_to_options():
    log_path = Path("logs/unified.log")
    dest_path = Path("strategies/options/logs/paper_trading/OPTIONS_20260410_indicators.csv")
    
    if not log_path.exists():
        print("❌ Log file not found")
        return

    print(f"🔍 Digging through logs: {log_path}")
    
    data = []
    # Use binary read + strings approach to avoid crash on special characters
    import subprocess
    try:
        # GSD: Use shell 'strings' to get clean text
        lines = subprocess.check_output(["strings", str(log_path)]).decode("utf-8", errors="ignore").splitlines()
        for line in lines:
            if "[FuturesMonitor] New Bar:" in line:
                # Extract timestamp and price
                match = re.search(r"New Bar: ([\d-]+ [\d:]+) close=([\d\.]+)", line)
                if match:
                    ts_str = match.group(1)
                    price = float(match.group(2))
                    data.append({"timestamp": ts_str, "close": price})
    except Exception as e:
        print(f"❌ Error reading log: {e}")
        return

    if not data:
        print("❌ No New Bar entries found in log")
        return

    df_new = pd.DataFrame(data)
    df_new["timestamp"] = pd.to_datetime(df_new["timestamp"])
    
    # Map to Options format
    rows = []
    for _, r in df_new.iterrows():
        price = float(r["close"])
        row = {
            "timestamp": r["timestamp"].strftime("%Y-%m-%d %H:%M:%S"),
            "session": 2,
            "score": 0.0,
            "side": "",
            "price_mtx": price,
            "strike": round(price / 100) * 100,
            "dte": 5.5,
            "mid_trend": "NORMAL",
            "iv": 0.25, "delta": 0.5, "gamma": 0.0001, "vega": 10.0,
            "vwap": price, "sqz_on": False
        }
        rows.append(row)

    df_opt_new = pd.DataFrame(rows)
    df_opt_new["timestamp"] = pd.to_datetime(df_opt_new["timestamp"])

    if dest_path.exists():
        try:
            existing = pd.read_csv(dest_path)
            existing["timestamp"] = pd.to_datetime(existing["timestamp"])
            combined = pd.concat([df_opt_new, existing]).drop_duplicates(subset=["timestamp"]).sort_values("timestamp")
        except:
            combined = df_opt_new
    else:
        combined = df_opt_new

    combined.to_csv(dest_path, index=False)
    print(f"✅ Extracted and synced {len(combined)} rows from log to {dest_path}")

if __name__ == "__main__":
    extract_log_to_options()
