#!/usr/bin/env python3
"""R-004 Episode Analysis v2 — compute spread from near/far bars."""
import csv, json
from statistics import mean, stdev

# Load near indicators (has 'close' column)
with open("logs/market_data/TMF_20260722_PAPER_indicators.csv") as f:
    near_rows = list(csv.DictReader(f))
print(f"Near rows: {len(near_rows)}")

# Load far data (has 'close' column)
with open("logs/market_data/TMF_far_20260722_PAPER.csv") as f:
    far_rows = list(csv.DictReader(f))
print(f"Far rows: {len(far_rows)}")

# Build far lookup by timestamp
far_by_ts = {}
for r in far_rows:
    try:
        far_by_ts[r["timestamp"]] = float(r["close"])
    except (ValueError, KeyError):
        pass
print(f"Far prices: {len(far_by_ts)}")

# Merge near + far by timestamp
merged = []
for r in near_rows:
    ts = r.get("timestamp", "")
    far_c = far_by_ts.get(ts)
    if far_c is None:
        continue
    try:
        near_c = float(r.get("close", 0))
        spread = far_c - near_c
        merged.append({"ts": ts, "near": near_c, "far": far_c, "spread": spread})
    except (ValueError, TypeError):
        pass

print(f"Merged rows: {len(merged)}")

# Compute spread Z-score (rolling 20-bar window)
window = 20
for i in range(len(merged)):
    if i < window:
        merged[i]["z"] = 0.0
        continue
    window_spreads = [merged[j]["spread"] for j in range(i-window, i)]
    mu = mean(window_spreads)
    sigma = stdev(window_spreads)
    merged[i]["z"] = (merged[i]["spread"] - mu) / sigma if sigma > 0 else 0.0

# Episode detection
entry_z = 2.0  # softer threshold since bars are 5-min
reset_z = 0.5
episodes = []
current = None

for i, d in enumerate(merged):
    abs_z = abs(d["z"])
    if current is None:
        if abs_z >= entry_z:
            current = {
                "start_idx": i, "start_ts": d["ts"],
                "start_z": d["z"], "start_spread": d["spread"],
                "end_idx": i, "end_ts": d["ts"],
                "max_z": abs_z, "direction": "WIDE" if d["z"] > 0 else "NARROW",
                "entries": 0,
            }
    else:
        current["end_idx"] = i
        current["end_ts"] = d["ts"]
        current["max_z"] = max(current["max_z"], abs_z)
        if abs_z < reset_z:
            episodes.append(current)
            current = None

if current:
    episodes.append(current)

print(f"\n=== EPISODES (|z| >= {entry_z}, reset < {reset_z}) ===")
print(f"Found {len(episodes)} episodes")

for i, ep in enumerate(episodes):
    start = ep["start_ts"]
    end = ep["end_ts"]
    duration_bars = ep["end_idx"] - ep["start_idx"] + 1
    spread_start = ep["start_spread"]
    # Find spread at end
    spread_end = merged[ep["end_idx"]]["spread"] if ep["end_idx"] < len(merged) else spread_start
    print(f"\nEpisode {i+1}: {ep['direction']}")
    print(f"  Time: {start} → {end}")
    print(f"  Bars: {duration_bars}")
    print(f"  Max |z|: {ep['max_z']:.2f}")
    print(f"  Spread: {spread_start:.0f} → {spread_end:.0f} (delta {spread_end-spread_start:+.0f})")

# Match entries to episodes
with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

entries = [o for o in orders if o.get("strategy") == "MTS_ENTRY"]

print(f"\n=== TRADE-EPISODE MAPPING ===")
for ei, ep in enumerate(episodes):
    matched = [e for e in entries if ep["start_ts"] <= str(e.get("created_at", ""))[:19] <= ep["end_ts"]]
    if matched:
        releases = []
        for m in matched:
            oid = m.get("order_id", "")
            # Find matching release
            for o in orders:
                if o.get("order_id", "").startswith(oid[:-4]) and o.get("strategy") == "MTS_RELEASE":
                    releases.append(o)
        print(f"  Episode {ei+1}: {len(matched)} entries, {len(releases)} releases")
        for m in matched:
            ts = str(m.get("created_at", ""))[:19]
            print(f"    ENTRY {ts} {m.get('symbol','')} {m.get('side','')} @ {m.get('avg_fill_price',0)}")

unmatched = [e for e in entries if not any(ep["start_ts"] <= str(e.get("created_at", ""))[:19] <= ep["end_ts"] for ep in episodes)]
if unmatched:
    print(f"\n  UNMATCHED: {len(unmatched)} entries")
