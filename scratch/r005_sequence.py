#!/usr/bin/env python3
"""R-005 Entry Sequence Study — does expectancy decay with entry number per episode?"""
import csv, json
from statistics import mean, stdev

# ── Config ──
ENTRY_Z = 2.0
RESET_Z = 0.5
ROLLING_WINDOW = 20

# ── Load Data ──
with open("logs/market_data/TMF_20260722_PAPER_indicators.csv") as f:
    near_rows = list(csv.DictReader(f))
with open("logs/market_data/TMF_far_20260722_PAPER.csv") as f:
    far_rows = list(csv.DictReader(f))
with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# ── Build far price map ──
far_map = {}
for r in far_rows:
    ts = r.get("timestamp", "")
    try:
        far_map[ts] = float(r["close"])
    except (ValueError, KeyError):
        pass

# ── Build merged spread + Z data ──
merged = []
for r in near_rows:
    ts = r.get("timestamp", "")
    if ts not in far_map:
        continue
    try:
        near_c = float(r.get("close", 0))
        far_c = far_map[ts]
        spread = far_c - near_c
        merged.append({"ts": ts, "near": near_c, "far": far_c, "spread": spread})
    except (ValueError, TypeError):
        pass

# Rolling Z-score (20-bar)
for i, d in enumerate(merged):
    if i < ROLLING_WINDOW:
        d["z"] = 0.0
        continue
    ws = [merged[j]["spread"] for j in range(i-ROLLING_WINDOW, i)]
    mu, s = mean(ws), stdev(ws)
    d["z"] = (d["spread"] - mu) / s if s > 0 else 0.0

# ── Episode detection ──
eps = []
cur = None
for i, d in enumerate(merged):
    az = abs(d["z"])
    if cur is None:
        if az >= ENTRY_Z:
            cur = {"start_i": i, "end_i": i, "ts0": d["ts"], "ts1": d["ts"],
                   "dir": "WIDE" if d["z"] > 0 else "NARROW", "z0": d["z"],
                   "max_z": az, "spreads": [d["spread"]],
                   "entry_count": 0, "entries": [], "entry_pnls": []}
    else:
        cur["end_i"] = i
        cur["ts1"] = d["ts"]
        cur["max_z"] = max(cur["max_z"], az)
        cur["spreads"].append(d["spread"])
        if az < RESET_Z:
            eps.append(cur)
            cur = None
if cur:
    eps.append(cur)

# ── Match entries to episodes (interval join with tolerance) ──
from datetime import datetime, timedelta

def parse_dt(s):
    try:
        if "T" in s:
            return datetime.strptime(s.split(".")[0], "%Y-%m-%dT%H:%M:%S")
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except (ValueError, IndexError):
        return None

entries = sorted(
    [o for o in orders if o.get("strategy") == "MTS_ENTRY"],
    key=lambda o: str(o.get("created_at", ""))
)

# Build bar lookup for interval matching
bar_times = [datetime.strptime(d["ts"], "%Y-%m-%d %H:%M:%S") for d in merged]

def find_episode(dt):
    """Find episode containing this timestamp."""
    for ep in eps:
        t0 = datetime.strptime(merged[ep["start_i"]]["ts"], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(merged[ep["end_i"]]["ts"], "%Y-%m-%d %H:%M:%S")
        if t0 <= dt <= t1 + timedelta(minutes=5):  # tolerance
            return ep
    return None

for e in entries:
    dt = parse_dt(str(e.get("created_at", "")))
    if dt is None:
        continue
    ep = find_episode(dt)
    if ep is None:
        continue
    ep["entry_count"] += 1
    ep["entries"].append(e)
    # Find the matching release and exit to compute PnL
    intent = e.get("intent_id", "")
    oid = e.get("order_id", "")
    if oid:
        rel = None
        ext = None
        for o in orders:
            o_strat = o.get("strategy", "")
            if o_strat == "MTS_RELEASE" and (o.get("parent_order_id", "") or "").startswith(oid[:10]):
                rel = o
            if o_strat == "MTS_EXIT" and (o.get("parent_order_id", "") or "").startswith(oid[:10]):
                ext = o

# ── Sort entries within each episode by time, assign sequence number
for ep in eps:
    ep["entries"].sort(key=lambda e: str(e.get("created_at", "")))
    for i, e in enumerate(ep["entries"]):
        e["_seq"] = i + 1  # entry number within episode

# ── Compute PnL by sequence position ──
# Strategy: ENTRY sell near + buy far
# PnL = (near_sell - near_buy) + (far_sell - far_buy)
# For RELEASE: one leg is closed
# For EXIT: remaining leg is closed

def compute_entry_pnl(pnl_info):
    """Compute estimated PnL for a single entry."""
    entry = pnl_info.get("entry")
    release = pnl_info.get("release")
    exit_o = pnl_info.get("exit")
    if not entry or not entry.get("avg_fill_price"):
        return None
    
    # Find sibling entry (other leg)
    intent = entry.get("intent_id", "")
    siblings = [o for o in entries if o.get("intent_id") == intent and o["order_id"] != entry["order_id"]]
    sibling = siblings[0] if siblings else None
    
    # Determine which leg is which
    entry_near = entry if "H6" in str(entry.get("symbol", "")) else sibling
    entry_far = sibling if "H6" in str(entry.get("symbol", "")) else entry
    
    if not entry_near or not entry_far:
        return None
    
    near_entry_price = entry_near.get("avg_fill_price") or 0
    far_entry_price = entry_far.get("avg_fill_price") or 0
    
    near_exit_price = None
    far_exit_price = None
    
    if release:
        rel_sym = release.get("symbol", "")
        rel_side = release.get("side", "")
        rel_price = release.get("avg_fill_price") or 0
        if "H6" in rel_sym:
            near_exit_price = rel_price  # near released (bought back)
        else:
            far_exit_price = rel_price  # far released (sold)
    
    if exit_o:
        ext_sym = exit_o.get("symbol", "")
        ext_side = exit_o.get("side", "")
        ext_price = exit_o.get("avg_fill_price") or 0
        if "H6" in ext_sym:
            near_exit_price = ext_price  # near exit
        else:
            far_exit_price = ext_price  # far exit
    
    if near_exit_price is None or far_exit_price is None:
        return None
    
    # Near: sold at entry, bought at exit → pnl = entry - exit
    near_pnl = near_entry_price - near_exit_price
    # Far: bought at entry, sold at exit → pnl = exit - entry
    far_pnl = far_exit_price - far_entry_price
    
    return near_pnl + far_pnl

# ── Report ──
print("=" * 60)
print("R-005 ENTRY SEQUENCE STUDY")
print("=" * 60)

# Gather all entries with sequence and PnL
all_sequences = []
for ep in eps:
    seq_num = 0
    for e in ep["entries"]:
        seq_num += 1
        e["_seq"] = seq_num
        # Find PnL
        pnl = None
        intent = e.get("intent_id", "")
        siblings = [o for o in entries if o.get("intent_id") == intent and o["order_id"] != e["order_id"]]
        if siblings:
            # Try to find release/exit for this entry pair
            oid = e.get("order_id", "")
            rel = None
            ext = None
            for o in orders:
                o_strat = o.get("strategy", "")
                if o_strat == "MTS_RELEASE" and o.get("parent_order_id", "").startswith(oid[:10]):
                    rel = o
                if o_strat == "MTS_EXIT" and o.get("parent_order_id", "").startswith(oid[:10]):
                    ext = o
            pnl_info = {"entry": e, "release": rel, "exit": ext}
            pnl = compute_entry_pnl(pnl_info)
        
        all_sequences.append({
            "episode_dir": ep["dir"],
            "episode_ts": ep["ts0"],
            "entry_seq": seq_num,
            "total_in_episode": ep["entry_count"],
            "entry_ts": str(e.get("created_at", ""))[:19],
            "symbol": e.get("symbol", ""),
            "side": e.get("side", ""),
            "price": e.get("avg_fill_price"),
            "pnl": pnl,
        })

# Group by sequence position
by_seq = {}
for s in all_sequences:
    k = s["entry_seq"]
    by_seq.setdefault(k, []).append(s)

print(f"\nTotal entries with sequence: {len(all_sequences)}")
print(f"Entries with PnL: {sum(1 for s in all_sequences if s['pnl'] is not None)}")
print(f"Entries without PnL: {sum(1 for s in all_sequences if s['pnl'] is None)}")

print(f"\n{'='*60}")
print(f"EXPECTANCY BY ENTRY SEQUENCE")
print(f"{'='*60}")

for seq in sorted(by_seq.keys()):
    items = by_seq[seq]
    pnls = [s["pnl"] for s in items if s["pnl"] is not None]
    if not pnls:
        print(f"\nEntry #{seq}: {len(items)} entries, 0 with PnL")
        continue
    avg = mean(pnls)
    med = sorted(pnls)[len(pnls)//2]
    wins = sum(1 for p in pnls if p > 0)
    print(f"\nEntry #{seq}: {len(items)} entries, {len(pnls)} with PnL")
    print(f"  Avg PnL: {avg:+.0f}")
    print(f"  Median:  {med:+.0f}")
    print(f"  Range:   {min(pnls):+.0f} → {max(pnls):+.0f}")
    print(f"  Wins:    {wins}/{len(pnls)} ({wins/len(pnls)*100:.0f}%)")
    # Show individual
    for s in items:
        pnl_str = f"{s['pnl']:+.0f}" if s['pnl'] is not None else "N/A"
        print(f"    {s['episode_ts']} [{s['episode_dir']}] {s['entry_ts']} {s['symbol']:6s} {s['side']:4s} @ {s['price']} PnL={pnl_str}")

# All entries total
print(f"\n{'='*60}")
print(f"ALL ENTRIES (total)")
print(f"{'='*60}")
all_pnls = [s["pnl"] for s in all_sequences if s["pnl"] is not None]
if all_pnls:
    print(f"Total: {len(all_pnls)} entries with PnL")
    print(f"Avg:   {mean(all_pnls):+.0f}")
    print(f"Wins:  {sum(1 for p in all_pnls if p>0)}/{len(all_pnls)} ({sum(1 for p in all_pnls if p>0)/len(all_pnls)*100:.0f}%)")
