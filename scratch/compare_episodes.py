#!/usr/bin/env python3
"""Compare recent vs earlier episodes."""
import json
from collections import Counter

with open("data/episode_dataset.jsonl") as f:
    eps = [json.loads(l) for l in f if l.strip()]

recent = [e for e in eps if e["trading_date"] >= "2026-07-21"]
earlier = [e for e in eps if e["trading_date"] < "2026-07-21"]

print("=== EARLIER (07-07 to 07-20) ===")
print(f"  Episodes: {len(earlier)}")
print(f"  Entries: {sum(e['entry_count'] for e in earlier)}")
print(f"  Avg entries/ep: {sum(e['entry_count'] for e in earlier)/len(earlier):.2f}")
print(f"  Distribution: {dict(sorted(Counter(e['entry_count'] for e in earlier).items()))}")
for d in sorted(set(e["trading_date"] for e in earlier)):
    de = [e for e in earlier if e["trading_date"] == d]
    print(f"    {d}: {len(de)} eps, {sum(e['entry_count'] for e in de)} entries")

print()
print("=== RECENT (07-21 to 07-22) ===")
print(f"  Episodes: {len(recent)}")
print(f"  Entries: {sum(e['entry_count'] for e in recent)}")
print(f"  Avg entries/ep: {sum(e['entry_count'] for e in recent)/len(recent):.2f}")
print(f"  Distribution: {dict(sorted(Counter(e['entry_count'] for e in recent).items()))}")
for d in sorted(set(e["trading_date"] for e in recent)):
    de = [e for e in recent if e["trading_date"] == d]
    print(f"    {d}: {len(de)} eps, {sum(e['entry_count'] for e in de)} entries")

# Spread expansion comparison
print()
print("=== SPREAD EXPANSION ===")
for label, group in [("Earlier", earlier), ("Recent", recent)]:
    if not group:
        continue
    for direction in ["WIDE", "NARROW"]:
        de = [e for e in group if e["direction"] == direction]
        if not de:
            continue
        if direction == "WIDE":
            deltas = [e["max_spread"] - e["start_spread"] for e in de]
        else:
            deltas = [e["start_spread"] - e["min_spread"] for e in de]
        avg_d = sum(deltas) / len(deltas)
        max_d = max(deltas)
        print(f"  {label} {direction}: {len(de)} eps, avg expansion={avg_d:.0f}, max={max_d:.0f}")
