#!/usr/bin/env python3
"""R-004 Episode Analysis — identify independent opportunities from 16 trades."""
import json, csv, sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Load indicators CSV
csv_path = "logs/market_data/TMF_20260722_PAPER_indicators.csv"
rows = []
with open(csv_path) as f:
    reader = csv.DictReader(f)
    for row in reader:
        rows.append(row)

print(f"Total rows: {len(rows)}")

# Find spread_z, near/far close columns
print(f"Columns: {list(rows[0].keys())[:15]}")

# Extract timestamps and spread_z
data = []
for r in rows:
    try:
        ts = r.get("ts", r.get("timestamp", ""))
        spread_z = float(r.get("spread_z", r.get("zscore", 0)))
        near = float(r.get("near_close", r.get("close", 0)))
        far = float(r.get("far_close", 0))
        data.append({"ts": ts, "z": spread_z, "near": near, "far": far})
    except (ValueError, TypeError):
        pass

print(f"Parsed {len(data)} rows with spread_z")

# Episode detection
# Episode = period when |z| >= entry_threshold (2.5) and spread is expanding
# Episode ends when |z| returns below reset threshold

entry_z = 2.5
reset_z = 1.0
episodes = []
current_episode = None

for i, d in enumerate(data):
    abs_z = abs(d["z"])
    
    if current_episode is None:
        # Check if we're entering an episode
        if abs_z >= entry_z:
            current_episode = {
                "start_idx": i,
                "start_ts": d["ts"],
                "start_z": d["z"],
                "end_idx": i,
                "end_ts": d["ts"],
                "max_z": abs_z,
                "min_z": abs_z,
                "entries": 0,
                "direction": "POSITIVE" if d["z"] > 0 else "NEGATIVE",
                "near_at_start": d["near"],
                "far_at_start": d["far"],
                "samples": 1,
            }
    else:
        # Update current episode
        current_episode["samples"] += 1
        current_episode["end_idx"] = i
        current_episode["end_ts"] = d["ts"]
        current_episode["max_z"] = max(current_episode["max_z"], abs_z)
        current_episode["min_z"] = min(current_episode["min_z"], abs_z)
        
        # Check if episode should end (z returns to neutral)
        if abs_z < reset_z:
            episodes.append(current_episode)
            current_episode = None

# Don't forget the last episode if still open
if current_episode is not None:
    episodes.append(current_episode)

print(f"\n=== EPISODES (|z| >= {entry_z}, reset at |z| < {reset_z}) ===")
print(f"Found {len(episodes)} episodes")

for i, ep in enumerate(episodes):
    start_dt = ep["start_ts"]
    end_dt = ep["end_ts"]
    dur_samples = ep["samples"]
    print(f"\nEpisode {i+1}: {ep['direction']}")
    print(f"  Time: {start_dt} → {end_dt}")
    print(f"  Duration: {dur_samples} samples")
    print(f"  Z range: {ep['min_z']:.2f} → {ep['max_z']:.2f}")
    print(f"  Start near: {ep['near_at_start']}, far: {ep['far_at_start']}")

# Now match trades to episodes
print(f"\n=== TRADE-EPISODE MAPPING ===")
# Load trades
with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# Get entry timestamps
entries = [o for o in orders if o.get("strategy") == "MTS_ENTRY"]
print(f"Total entries: {len(entries)}")

# Map each entry to an episode
for ep_idx, ep in enumerate(episodes):
    ep_start = ep["start_ts"]
    ep_end = ep["end_ts"]
    matched = []
    for e in entries:
        et = str(e.get("created_at", ""))
        if ep_start <= et <= ep_end:
            matched.append(e)
    if matched:
        print(f"  Episode {ep_idx+1}: {len(matched)} entries")
        for m in matched:
            ts = str(m.get("created_at", ""))
            sym = m.get("symbol", "")
            side = m.get("side", "")
            price = m.get("avg_fill_price", 0)
            print(f"    {ts} {sym:6s} {side:4s} @ {price}")

# Entries that didn't match any episode
unmatched = []
for e in entries:
    et = str(e.get("created_at", ""))
    found = False
    for ep in episodes:
        if ep["start_ts"] <= et <= ep["end_ts"]:
            found = True
            break
    if not found:
        unmatched.append(e)

if unmatched:
    print(f"\n  UNMATCHED entries: {len(unmatched)}")
    for m in unmatched:
        print(f"    {str(m.get('created_at',''))} {m.get('symbol','')} {m.get('side','')} @ {m.get('avg_fill_price',0)}")
