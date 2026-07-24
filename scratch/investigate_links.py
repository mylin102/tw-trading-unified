#!/usr/bin/env python3
"""Investigate how entries link to releases/exits."""
import json

with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# Find a matching entry-release pair by order_id proximity
entries = [o for o in orders if o.get("strategy") == "MTS_ENTRY"]
releases = [o for o in orders if o.get("strategy") == "MTS_RELEASE"]
exits = [o for o in orders if o.get("strategy") == "MTS_EXIT"]

# Order IDs are like ORD-20260722-000001, ORD-20260722-000002
# Sequential numbering suggests chronological order
# An ENTRY pair (NEAR + FAR) has sequential IDs
# Then RELEASE and EXIT follow

print("=== ORDER SEQUENCE SAMPLE ===")
for o in orders[:20]:
    print(f"  {o['order_id']:32s} {o.get('strategy',''):16s} {o.get('symbol',''):6s} {str(o.get('avg_fill_price','')):>8s}")

print("\n=== Checking fills for cross-references ===")
# Some orders have 'fills' array with deal_ids
# Check if release fills reference entry order_ids
for rel in releases[:3]:
    rel_id = rel["order_id"]
    fills = rel.get("fills", [])
    print(f"\nRELEASE {rel_id}: {len(fills)} fills")
    for f in fills:
        print(f"  deal_id: {f.get('deal_id','')}")

# The deal ID format is: deal_ORDERID_...
# e.g., deal_ORD-20260722-000067_20260722_115424_222608
# The order_id in the deal_id is the ORDER that produced this fill
# So a RELEASE with deal_id containing ORD-20260722-000067
# was produced by order ORD-20260722-000067

# But which ENTRY does this RELEASE correspond to?
# The TMF strategy creates a spread entry with TWO orders (near + far)
# Then RELEASE closes ONE leg
# The mapping would be: entry order -> release order -> exit order

# Let me check by matching intent_id
print("\n=== INTENT_ID ANALYSIS ===")
by_iid = {}
for o in orders:
    iid = o.get("intent_id", "")
    by_iid.setdefault(iid, []).append(o)

for iid, items in sorted(by_iid.items()):
    strats = [o.get("strategy","?") for o in items]
    print(f"  {iid[-16:]}: {strats}")

# Maybe the mapping is via raw_events or broker references
print("\n=== CHECKING RAW_EVENTS ===")
for rel in releases[:1]:
    raw = rel.get("raw_events", [])
    print(f"RELEASE {rel['order_id']}: {len(raw)} raw events")
    for ev in raw[:3]:
        print(f"  {ev.get('type','')}: from {ev.get('from_status','')} to {ev.get('to_status','')}")
