#!/usr/bin/env python3
"""R-008 Volatility Regime Shift — compare spread ATR before and after 07-21."""
import csv
import json
from pathlib import Path
from statistics import mean, stdev

MARKET_DATA = Path("logs/market_data")

# Collect ATR and spread data for each trading date
periods = {"earlier": [], "recent": []}

for f in sorted(MARKET_DATA.glob("TMF_*_PAPER_indicators.csv")):
    date_str = f.stem.split("_")[1]  # 20260722
    period = "recent" if date_str >= "20260721" else "earlier"
    
    with open(f) as fh:
        rows = list(csv.DictReader(fh))
    
    atr_vals = []
    spreads = []
    for r in rows:
        try:
            atr = float(r.get("atr", 0) or 0)
            if atr > 0:
                atr_vals.append(atr)
        except (ValueError, TypeError):
            pass
    
    if atr_vals:
        periods[period].append({
            "date": f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}",
            "bars": len(rows),
            "avg_atr": mean(atr_vals),
            "max_atr": max(atr_vals),
            "atr_at_90pct": sorted(atr_vals)[int(len(atr_vals) * 0.9)],
        })

print("=== R-008 VOLATILITY REGIME SHIFT ===")
print()

for label, data in [("Earlier (pre-07-21)", periods["earlier"]), ("Recent (post-07-21)", periods["recent"])]:
    if not data:
        continue
    avg_atrs = [d["avg_atr"] for d in data]
    max_atrs = [d["max_atr"] for d in data]
    print(f"{label}:")
    print(f"  Trading days: {len(data)}")
    print(f"  Avg ATR range: {min(avg_atrs):.1f} - {max(avg_atrs):.1f}")
    print(f"  Mean of daily avg ATR: {mean(avg_atrs):.1f}")
    print(f"  Max ATR range: {min(max_atrs):.0f} - {max(max_atrs):.0f}")
    print(f"  Mean of daily max ATR: {mean(max_atrs):.0f}")
    print()

# Show daily breakdown
print("=== DAILY ATR BREAKDOWN ===")
print(f"{'Date':<12} {'Period':<10} {'Bars':>5} {'AvgATR':>8} {'MaxATR':>8} {'ATR90':>8}")
print("-" * 55)
for label, data in [("Earlier", periods["earlier"]), ("Recent", periods["recent"])]:
    for d in data:
        print(f"{d['date']:<12} {label:<10} {d['bars']:>5} {d['avg_atr']:>8.1f} {d['max_atr']:>8.0f} {d['atr_at_90pct']:>8.0f}")
