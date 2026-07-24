#!/usr/bin/env python3
"""Analyze TMF trades."""
import json

with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

print(f"Total orders: {len(orders)}")

# Show unique field names
keys = set()
for item in orders:
    keys.update(item.keys())
print(f"Fields: {sorted(keys)}")

# Show last 3 orders
for item in orders[-3:]:
    print(json.dumps(item, indent=2, ensure_ascii=False)[:600])
    print("---")
