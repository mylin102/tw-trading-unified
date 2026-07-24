"""Analyze MTS PnL, filtering out known PnL calculation artifacts."""
import json
import sys
from collections import defaultdict, OrderedDict

fills_path = sys.argv[1] if len(sys.argv) > 1 else "logs/mts_trade_fills.jsonl"

trades = defaultdict(lambda: {"entries": [], "releases": [], "exits": [], "spread_pnl": 0})

with open(fills_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        r = json.loads(line)
        tid = r["trade_id"]
        fill_type = r["fill_type"].lower()
        key_map = {"entry": "entries", "release": "releases", "exit": "exits"}
        if fill_type in key_map:
            trades[tid][key_map[fill_type]].append(r)
        if r.get("spread_pnl") is not None:
            trades[tid]["spread_pnl"] = r["spread_pnl"]

results = []
for tid, data in sorted(trades.items(),
                         key=lambda x: x[1]["entries"][0]["timestamp"]
                         if x[1]["entries"] else ""):
    entries = data.get("entries", [])
    releases = data.get("releases", [])
    exits = data.get("exits", [])
    spread_pnl = data.get("spread_pnl", 0)

    total_realized = 0
    for r in releases + exits:
        pnl = r.get("realized_pnl")
        if pnl is not None:
            total_realized += pnl

    ts = entries[0]["timestamp"] if entries else "unknown"
    date = ts[:10]
    session = entries[0].get("session", "?") if entries else "?"
    spread_z = entries[0].get("spread_z", "?") if entries else "?"

    entry_count = len(entries)
    release_count = len(releases)
    exit_count = len(exits)

    # Detect PnL calculation artifacts:
    # 1. total realized > 100000 (impossible for 1-contract TMF)
    # 2. More than 1 release on same trade (lifecycle error)
    # 3. More than 1 exit on same trade (lifecycle error)
    is_artifact = False
    artifact_reason = ""
    if abs(total_realized) > 100000:
        is_artifact = True
        artifact_reason = "pnl_overflow"
    if release_count > 1:
        is_artifact = True
        artifact_reason = f"multi_release({release_count})"
    if exit_count > 1:
        is_artifact = True
        artifact_reason = f"multi_exit({exit_count})"
    if entry_count != 2:
        is_artifact = True
        artifact_reason = f"odd_entries({entry_count})"

    results.append({
        "trade_id": tid,
        "date": date,
        "total_realized": total_realized,
        "spread_pnl": spread_pnl,
        "session": session,
        "spread_z": spread_z,
        "entries": entry_count,
        "releases": release_count,
        "exits": exit_count,
        "is_artifact": is_artifact,
        "artifact_reason": artifact_reason,
    })

# Split into clean vs artifact
clean = [r for r in results if not r["is_artifact"]]
artifacts = [r for r in results if r["is_artifact"]]

print("=" * 65)
print("CLEAN TRADES (no lifecycle anomalies)")
print("=" * 65)

daily_clean = OrderedDict()
for r in clean:
    d = r["date"]
    if d not in daily_clean:
        daily_clean[d] = {"trades": 0, "pnl": 0, "wins": 0, "losses": 0}
    daily_clean[d]["trades"] += 1
    daily_clean[d]["pnl"] += r["total_realized"]
    if r["total_realized"] > 0:
        daily_clean[d]["wins"] += 1
    else:
        daily_clean[d]["losses"] += 1

print(f"{'Date':14s} {'Trades':7s} {'W/L':8s} {'Realized':>9s} {'Cumul':>9s}")
print("-" * 55)
cumulative_clean = 0
for d in sorted(daily_clean.keys()):
    data = daily_clean[d]
    cumulative_clean += data["pnl"]
    wl = f"{data['wins']}/{data['losses']}"
    print(f"{d:14s} {data['trades']:4d}    {wl:6s} {data['pnl']:8.0f}  {cumulative_clean:8.0f}")

print()
total_clean_pnl = sum(r["total_realized"] for r in clean)
wins_clean = sum(1 for r in clean if r["total_realized"] > 0)
total_clean = len(clean)
print(f"Clean trades: {total_clean}")
print(f"Clean total PnL: {total_clean_pnl:.0f}")
print(f"Clean win rate: {wins_clean}/{total_clean} ({wins_clean/total_clean*100:.0f}%)")
print()

# Biggest losers among clean trades
print("Clean trades with largest losses:")
big_losers = sorted([r for r in clean if r["total_realized"] < 0],
                    key=lambda x: x["total_realized"])[:15]
for r in big_losers:
    print(f"  {r['date']} {r['trade_id'][:25]:26s} pnl={r['total_realized']:+7.0f}  "
          f"z={r['spread_z']}  {r['session']}")

print()

# Also show artifacts for reference
print("=" * 65)
print(f"ARTIFACT TRADES (excluded: {len(artifacts)})")
print("=" * 65)
for r in sorted(artifacts, key=lambda x: x["total_realized"]):
    print(f"  {r['date']} {r['trade_id'][:25]:26s} pnl={r['total_realized']:+9.0f}  "
          f"reason={r['artifact_reason']}  z={r['spread_z']}")
