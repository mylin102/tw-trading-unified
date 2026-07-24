#!/usr/bin/env python3
"""R-004 v3 — proper episode analysis with timestamp alignment."""
import csv, json
from statistics import mean, stdev

# Load data
with open("logs/market_data/TMF_20260722_PAPER_indicators.csv") as f:
    near_rows = list(csv.DictReader(f))
with open("logs/market_data/TMF_far_20260722_PAPER.csv") as f:
    far_rows = list(csv.DictReader(f))
with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# Build far lookup
far_by_ts = {}
for r in far_rows:
    try:
        far_by_ts[r["timestamp"]] = float(r["close"])
    except (ValueError, KeyError):
        pass

# Merge near + far
merged = []
for r in near_rows:
    ts = r.get("timestamp", "")
    if ts not in far_by_ts:
        continue
    try:
        near_c = float(r.get("close", 0))
        far_c = far_by_ts[ts]
        spread = far_c - near_c
        merged.append({"ts": ts, "near": near_c, "far": far_c, "spread": spread})
    except (ValueError, TypeError):
        pass

# Rolling Z-score (20-bar)
window = 20
for i, d in enumerate(merged):
    if i < window:
        d["z"] = 0.0
        continue
    ws = [merged[j]["spread"] for j in range(i-window, i)]
    mu, s = mean(ws), stdev(ws)
    d["z"] = (d["spread"] - mu) / s if s > 0 else 0.0

# Episodes
entry_z, reset_z = 2.0, 0.5
eps = []
cur = None
for i, d in enumerate(merged):
    az = abs(d["z"])
    if cur is None:
        if az >= entry_z:
            cur = {"start": i, "ts0": d["ts"], "z0": d["z"], "spr0": d["spread"],
                   "end": i, "ts1": d["ts"], "maxz": az, "minz": az,
                   "dir": "WIDE" if d["z"] > 0 else "NARROW", "spreads": [d["spread"]]}
    else:
        cur["end"] = i; cur["ts1"] = d["ts"]
        cur["maxz"] = max(cur["maxz"], az); cur["minz"] = min(cur["minz"], az)
        cur["spreads"].append(d["spread"])
        if az < reset_z:
            eps.append(cur); cur = None
if cur:
    eps.append(cur)

# Match entries to bars
entries = sorted(
    [o for o in orders if o.get("strategy") == "MTS_ENTRY"],
    key=lambda o: str(o.get("created_at", ""))
)

# For each entry, find the nearest bar (within 5 min)
def find_bar(ts_iso):
    """Find nearest bar timestamp (round down to 5 min)."""
    # Parse ISO timestamp
    parts = ts_iso.replace("T", " ").split(".")[0].split(" ")
    if len(parts) != 2:
        return None
    time_parts = parts[1].split(":")
    if len(time_parts) < 2:
        return None
    h, m = int(time_parts[0]), int(time_parts[1])
    m5 = (m // 5) * 5
    return f"{parts[0]} {h:02d}:{m5:02d}:00"

# Group entries by bar
entries_by_bar = {}
for e in entries:
    ts = str(e.get("created_at", ""))
    bar_ts = find_bar(ts)
    if bar_ts:
        entries_by_bar.setdefault(bar_ts, []).append(e)

# Map episodes to entries
print("=== R-004 EPISODE ANALYSIS ===")
print(f"Bars: {len(merged)}, Episodes: {len(eps)}, Entries: {len(entries)}")

for ei, ep in enumerate(eps):
    matched = []
    for bar_ts, ents in entries_by_bar.items():
        if ep["ts0"] <= bar_ts <= ep["ts1"]:
            matched.extend(ents)
    
    if not matched:
        continue
    
    # Count releases for these entries
    rel_count = 0
    for m in matched:
        oid = m.get("order_id", "")
        prefix = oid[:-5] if len(oid) > 5 else oid
        for o in orders:
            if o.get("order_id","").startswith(prefix) and o.get("strategy") == "MTS_RELEASE":
                rel_count += 1
    
    spr_start = ep["spreads"][0]
    spr_end = ep["spreads"][-1]
    dur = (ep["end"] - ep["start"] + 1) * 5  # minutes
    
    print(f"\nEpisode {ei+1}: {ep['dir']}  ({ep['ts0']} → {ep['ts1']}, {dur}min)")
    print(f"  Z: {ep['z0']:.1f} → max {ep['maxz']:.1f} → min {ep['minz']:.1f}")
    print(f"  Spread: {spr_start:.0f} → {spr_end:.0f} (delta {spr_end-spr_start:+.0f})")
    print(f"  Entries: {len(matched)}, Releases: {rel_count}")
    for m in matched:
        ts = str(m.get("created_at", ""))[:19]
        sym = m.get("symbol", "")
        side = m.get("side", "")
        price = m.get("avg_fill_price", 0)
        print(f"    {ts} {sym:6s} {side:4s} @ {price}")

# Summary
print(f"\n=== SUMMARY ===")
total_ep_entries = 0
total_ep_releases = 0
ep_with_entries = 0
for ei, ep in enumerate(eps):
    matched = []
    for bar_ts, ents in entries_by_bar.items():
        if ep["ts0"] <= bar_ts <= ep["ts1"]:
            matched.extend(ents)
    if matched:
        ep_with_entries += 1
        total_ep_entries += len(matched)
        rel_count = 0
        for m in matched:
            oid = m.get("order_id", "")
            prefix = oid[:-5] if len(oid) > 5 else oid
            for o in orders:
                if o.get("order_id","").startswith(prefix) and o.get("strategy") == "MTS_RELEASE":
                    rel_count += 1
        total_ep_releases += rel_count

print(f"Episodes with entries: {ep_with_entries}")
print(f"Total entries in episodes: {total_ep_entries}")
print(f"Total releases in episodes: {total_ep_releases}")
print(f"Avg entries/episode: {total_ep_entries/ep_with_entries:.1f}" if ep_with_entries else "")
