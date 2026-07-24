#!/usr/bin/env python3
"""Full TMF trade analysis for 2026-07-22."""
import json

with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# Group by strategy
by_strategy = {}
for o in orders:
    s = o.get("strategy", "")
    by_strategy.setdefault(s, []).append(o)

print("=== ORDER STRATEGIES ===")
for s, items in sorted(by_strategy.items()):
    print(f"  {s}: {len(items)}")

# Find complete spread entries (NEAR ENTRY + FAR ENTRY pairs)
# Then track their RELEASE and EXIT
entries = [o for o in orders if o.get("strategy") == "MTS_ENTRY"]
releases = [o for o in orders if o.get("strategy") == "MTS_RELEASE"]
exits = [o for o in orders if o.get("strategy") == "MTS_EXIT"]
single_legs = [o for o in orders if o.get("strategy") == "MTS_SINGLE_LEG"]

print(f"\n=== TRADE FLOW ===")
print(f"  ENTRY: {len(entries)} orders")
print(f"  RELEASE: {len(releases)} orders")
print(f"  SINGLE_LEG: {len(single_legs)} orders")
print(f"  EXIT: {len(exits)} orders")

# Show each entry with its fills and PnL
print(f"\n=== ENTRY PRICES ===")
for o in entries:
    sym = o.get("symbol", "?")
    side = o.get("side", "?")
    price = o.get("avg_fill_price")
    ts = str(o.get("created_at", ""))[:19]
    oid = o.get("order_id", "")[-12:]
    print(f"  {ts} {oid} {sym:6s} {side:4s} @ {price}")

print(f"\n=== RELEASE PRICES ===")
for o in releases:
    sym = o.get("symbol", "?")
    side = o.get("side", "?")
    price = o.get("avg_fill_price")
    ts = str(o.get("created_at", ""))[:19]
    oid = o.get("order_id", "")[-12:]
    print(f"  {ts} {oid} {sym:6s} {side:4s} @ {price}")

print(f"\n=== SINGLE_LEG PRICES ===")
for o in single_legs:
    sym = o.get("symbol", "?")
    side = o.get("side", "?")
    price = o.get("avg_fill_price")
    ts = str(o.get("created_at", ""))[:19]
    oid = o.get("order_id", "")[-12:]
    print(f"  {ts} {oid} {sym:6s} {side:4s} @ {price}")

print(f"\n=== EXIT PRICES ===")
for o in exits:
    sym = o.get("symbol", "?")
    side = o.get("side", "?")
    price = o.get("avg_fill_price")
    ts = str(o.get("created_at", ""))[:19]
    oid = o.get("order_id", "")[-12:]
    print(f"  {ts} {oid} {sym:6s} {side:4s} @ {price}")

# Show unrealized PnL trends
print(f"\n=== ORDER TIMELINE ===")
for o in orders:
    ts = str(o.get("created_at", ""))[:19]
    oid = o.get("order_id", "")[-12:]
    sym = o.get("symbol", "?")
    side = o.get("side", "?")
    strat = o.get("strategy", "?")
    price = o.get("avg_fill_price")
    upnl = o.get("unrealized_pnl", "")
    print(f"  {ts} {oid} {strat:16s} {sym:6s} {side:4s} @ {price} unrealized={upnl}")

# Summarize net PnL
total_pnl = 0
pnl_orders = [o for o in orders if o.get("unrealized_pnl") is not None]
print(f"\n=== PnL SUMMARY ===")
print(f"  Orders with PnL: {len(pnl_orders)}")
# Find the final PnL for each completed position
final_pnls = [o for o in orders if o.get("status") == "filled" and o.get("unrealized_pnl") is not None and o.get("strategy") in ("MTS_EXIT", "MTS_RELEASE")]
for o in final_pnls:
    ts = str(o.get("updated_at", o.get("created_at", "")))[:19]
    print(f"  {ts} {o['symbol']:6s} {o['side']:4s} pnl={o['unrealized_pnl']:>8.0f} strategy={o['strategy']}")
