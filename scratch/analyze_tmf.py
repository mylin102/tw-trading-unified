#!/usr/bin/env python3
"""Analyze TMF trades for today."""
import json
from collections import Counter, defaultdict

with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# Summarize
print(f"Total order records: {len(orders)}")

# Find complete trade lifecycles
# Each trade has: ENTRY (NEAR + FAR), RELEASE (one leg), TRAIL (remaining leg)
trades = []
for o in orders:
    action = o.get("action", "")
    if action == "ENTRY" and o.get("leg") == "NEAR":
        trades.append({"entry_near": o})
    elif action == "ENTRY" and o.get("leg") == "FAR":
        if trades:
            trades[-1]["entry_far"] = o

# Show PnL from last entries
print(f"\n=== RECENT ENTRIES ===")
for o in orders[-20:]:
    action = o.get("action", "?")
    side = o.get("side", "?")
    price = o.get("price", "?")
    pnl = o.get("pnl", "")
    ts = str(o.get("ts", ""))[:19]
    leg = o.get("leg", "")
    deal_id = str(o.get("deal_id", ""))[-12:]
    print(f"  {ts} {action:8s} {leg:4s} {side:4s} @ {str(price):>8s}  deal={deal_id}")

# Find completion records (deals with realized PnL)
print(f"\n=== PnL SUMMARY ===")
total_pnl = 0
for o in orders:
    if "pnl" in o:
        try:
            pnl = float(o["pnl"])
            total_pnl += pnl
            leg = o.get("leg", "")
            action = o.get("action", "")
            ts = str(o.get("ts", ""))[:19]
            print(f"  {ts} {action:8s} {leg:4s} pnl={pnl:>8.0f}")
        except (ValueError, TypeError):
            pass
print(f"\nTotal PnL: {total_pnl:.0f}")
