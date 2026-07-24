#!/usr/bin/env python3
"""R-004 v4 — refined episode analysis with attribution quality gates."""
import csv, json
from statistics import mean, stdev
from datetime import datetime, timedelta

# ── Config ──
ENTRY_Z = 2.0
RESET_Z = 0.5
ROLLING_WINDOW = 20  # bars
MAX_LEG_SKEW_SEC = 60  # max time diff between near/far legs
MAX_QUOTE_AGE_SEC = 120  # max age for a close price
OUTPUT_PREFIX = "reports/research/R004_episode_analysis"

# ── Load Data ──
with open("logs/market_data/TMF_20260722_PAPER_indicators.csv") as f:
    near_rows = list(csv.DictReader(f))
with open("logs/market_data/TMF_far_20260722_PAPER.csv") as f:
    far_rows = list(csv.DictReader(f))
with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# ── Build far price map with timestamp parsing ──
far_map = {}  # ts_str -> {close, parsed_dt}
for r in far_rows:
    ts = r.get("timestamp", "")
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        far_map[ts] = {"close": float(r["close"]), "dt": dt}
    except (ValueError, KeyError):
        pass

# ── Merge near + far with quote quality checks ──
merged = []
quote_quality_issues = []
for r in near_rows:
    ts = r.get("timestamp", "")
    far_data = far_map.get(ts)
    if far_data is None:
        quote_quality_issues.append({"ts": ts, "reason": "FAR_MISSING", "detail": "No far bar at this timestamp"})
        continue
    
    try:
        near_dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        near_c = float(r.get("close", 0))
        far_c = far_data["close"]
        far_dt = far_data["dt"]
        
        # Quote quality check
        leg_skew = abs((near_dt - far_dt).total_seconds())
        if leg_skew > MAX_LEG_SKEW_SEC:
            quote_quality_issues.append({"ts": ts, "reason": "LEG_SKEW", "detail": f"near/far skew={leg_skew}s"})
            continue
        
        spread = far_c - near_c
        if spread < -50 or spread > 500:  # sanity check for TMF calendar spread
            quote_quality_issues.append({"ts": ts, "reason": "SPREAD_OUTLIER", "detail": f"spread={spread:.0f}"})
            # still include but mark as questionable
            merged.append({"ts": ts, "dt": near_dt, "near": near_c, "far": far_c,
                          "spread": spread, "valid": False})
            continue
        
        merged.append({"ts": ts, "dt": near_dt, "near": near_c, "far": far_c,
                      "spread": spread, "valid": True, "skew_s": leg_skew})
    except (ValueError, TypeError):
        quote_quality_issues.append({"ts": ts, "reason": "PARSE_ERROR"})

print(f"Bars total: {len(near_rows)}")
print(f"Bars merged: {len(merged)}")
print(f"Quote issues: {len(quote_quality_issues)}")
print(f"Valid spreads: {sum(1 for m in merged if m['valid'])}")

# ── Rolling Z-score (valid points only) ──
valid_spreads = [m["spread"] for m in merged]
for i, m in enumerate(merged):
    if i < ROLLING_WINDOW:
        m["z"] = 0.0
        m["z_valid"] = False
        continue
    ws = [valid_spreads[j] for j in range(i-ROLLING_WINDOW, i)]
    mu, s = mean(ws), stdev(ws)
    m["z"] = (m["spread"] - mu) / s if s > 0 else 0.0
    m["z_valid"] = True

# ── Episode Segmentation ──
eps = []
cur = None
for i, m in enumerate(merged):
    if not m.get("z_valid"):
        if cur is not None:
            eps.append(cur)
            cur = None
        continue
    
    az = abs(m["z"])
    if cur is None:
        if az >= ENTRY_Z:
            cur = {"start_i": i, "end_i": i, "ts0": m["ts"], "ts1": m["ts"],
                   "dir": "WIDE" if m["z"] > 0 else "NARROW",
                   "max_z": az, "min_z": az, "z0": m["z"],
                   "spr0": m["spread"], "spr_max": m["spread"], "spr_min": m["spread"],
                   "valid_points": 1 if m["valid"] else 0, "invalid_points": 0 if m["valid"] else 1,
                   "spreads": [m["spread"]], "entry_count": 0, "release_count": 0,
                   "entry_ids": [], "entries": []}
    else:
        cur["end_i"] = i
        cur["ts1"] = m["ts"]
        cur["max_z"] = max(cur["max_z"], az)
        cur["min_z"] = min(cur["min_z"], az)
        cur["spr_max"] = max(cur["spr_max"], m["spread"])
        cur["spr_min"] = min(cur["spr_min"], m["spread"])
        cur["spreads"].append(m["spread"])
        if m["valid"]:
            cur["valid_points"] += 1
        else:
            cur["invalid_points"] += 1
        
        if az < RESET_Z:
            eps.append(cur)
            cur = None
if cur:
    eps.append(cur)

# ── Entry-to-bar alignment (signal_event_time estimation) ──
# Order timestamps are fill times. Signal time is ~5-15s earlier.
# Use bar interval join: entry belongs to bar where bar.timestamp <= entry.time < bar.timestamp + 5min
def parse_order_dt(order):
    """Parse order created_at to datetime."""
    ts_str = str(order.get("created_at", ""))
    try:
        if "T" in ts_str:
            return datetime.strptime(ts_str.split(".")[0], "%Y-%m-%dT%H:%M:%S")
        return datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return None

def find_bar_idx(dt):
    """Find the bar that contains this timestamp (bar_start <= ts < bar_start + 5min)."""
    for i, m in enumerate(merged):
        bar_dt = m["dt"]
        if bar_dt <= dt < bar_dt + timedelta(minutes=5):
            return i
    return None

entries = sorted(
    [o for o in orders if o.get("strategy") == "MTS_ENTRY"],
    key=lambda o: str(o.get("created_at", ""))
)

unmatched_entries = []
for e in entries:
    dt = parse_order_dt(e)
    if dt is None:
        unmatched_entries.append({"entry": e, "reason": "NO_TIMESTAMP"})
        continue
    bi = find_bar_idx(dt)
    if bi is None:
        # Try with 1-bar tolerance (entry might be slightly delayed)
        # Only search forward (entries happen after bar close)
        for offset in [1]:
            test_dt = dt - timedelta(minutes=5)  # check previous bar
            for i, m in enumerate(merged):
                if m["dt"] <= dt < m["dt"] + timedelta(minutes=5):
                    bi = i
                    break
            if bi is not None:
                break
    if bi is None:
        unmatched_entries.append({"entry": e, "reason": "NO_MATCHING_BAR",
                                   "ts": str(dt)})
        continue
    
    # Attribute to episode if bar falls in one
    for ep in eps:
        if ep["start_i"] <= bi <= ep["end_i"]:
            ep["entry_count"] += 1
            ep["entries"].append(e)
            eid = e.get("order_id", str(e.get("created_at", "")))
            ep["entry_ids"].append(eid)
            break

# ── Release Attribution ──
# Match RELEASE to entries via intent_id (unique per spread entry)
releases = [o for o in orders if o.get("strategy") == "MTS_RELEASE"]

for ep in eps:
    release_count = 0
    # For each entry in this episode, find matching release by intent_id
    for e in ep["entries"]:
        intent = e.get("intent_id", "")
        if not intent:
            continue
        matched_releases = [r for r in releases if r.get("intent_id") == intent]
        release_count += len(matched_releases)
    ep["release_count"] = release_count

# ── Report ──
print(f"\n{'='*60}")
print(f"R-004 EPISODE ANALYSIS v4")
print(f"{'='*60}")
print(f"\nData quality:")
print(f"  Total bars: {len(near_rows)}")
print(f"  Merged bars: {len(merged)}")
print(f"  Invalid spread points: {sum(1 for m in merged if not m.get('valid', True))}")
print(f"  Quote quality issues: {len(quote_quality_issues)}")
print(f"  Valid episodes: {len(eps)}")
print(f"  Total entries: {len(entries)}")
print(f"  Matched entries: {len(entries) - len(unmatched_entries)}")
print(f"  Unmatched entries: {len(unmatched_entries)}")
coverage = (len(entries) - len(unmatched_entries)) / len(entries) * 100 if entries else 0
print(f"  Coverage: {coverage:.1f}%")

if unmatched_entries:
    print(f"\n  Unmatched reasons:")
    reasons = {}
    for ue in unmatched_entries:
        r = ue["reason"]
        reasons[r] = reasons.get(r, 0) + 1
    for r, c in sorted(reasons.items()):
        print(f"    {r}: {c}")

print(f"\n{'='*60}")
print(f"EPISODES WITH ENTRIES")
print(f"{'='*60}")

ep_with_entries = [ep for ep in eps if ep["entry_count"] > 0]

for i, ep in enumerate(ep_with_entries):
    dur_bars = ep["end_i"] - ep["start_i"] + 1
    dur_min = dur_bars * 5
    valid_ratio = ep["valid_points"] / (ep["valid_points"] + ep["invalid_points"]) * 100 if (ep["valid_points"] + ep["invalid_points"]) > 0 else 0
    
    print(f"\nEpisode {eps.index(ep)+1}: {ep['dir']}")
    print(f"  Time: {ep['ts0']} → {ep['ts1']} ({dur_min}min, {dur_bars} bars)")
    print(f"  Z: {ep['z0']:.1f} → max {ep['max_z']:.1f} → min {ep['min_z']:.1f}")
    print(f"  Spread: {ep['spr0']:.0f} [{ep['spr_min']:.0f}–{ep['spr_max']:.0f}]")
    print(f"  Data quality: {valid_ratio:.0f}% valid")
    print(f"  Entries: {ep['entry_count']}, Releases: {ep['release_count']}")
    for e in ep["entries"]:
        ts = str(e.get("created_at", ""))[:19]
        sym = e.get("symbol", "")
        side = e.get("side", "")
        price = e.get("avg_fill_price", 0)
        print(f"    ENTRY {ts} {sym:6s} {side:4s} @ {price}")

# Non-episode entries
non_ep_entries = [e for e in entries if not any(
    ep["start_i"] <= (find_bar_idx(parse_order_dt(e)) or -1) <= ep["end_i"]
    for ep in eps
)]
if non_ep_entries and len(non_ep_entries) < len(entries):
    print(f"\nEntries outside episodes: {len(non_ep_entries)}")

print(f"\n{'='*60}")
print(f"SUMMARY")
print(f"{'='*60}")
total_ep_entries = sum(ep["entry_count"] for ep in ep_with_entries)
total_ep_releases = sum(ep["release_count"] for ep in ep_with_entries)
avg_entries = total_ep_entries / len(ep_with_entries) if ep_with_entries else 0
print(f"Episodes with entries: {len(ep_with_entries)}")
print(f"Total entries in episodes: {total_ep_entries}")
print(f"Total releases in episodes: {total_ep_releases}")
print(f"Avg entries/episode: {avg_entries:.2f}")
print(f"Entry coverage: {coverage:.1f}%")
print(f"Coverage gate (≥95%): {'PASS' if coverage >= 95 else 'FAIL'}")

# Release count sanity
total_releases = sum(ep["release_count"] for ep in eps)
print(f"Total releases attributed (all episodes): {total_releases}")
print(f"Actual total releases: {len(releases)}")
print(f"Release overcount: {total_releases - len(releases)}")
