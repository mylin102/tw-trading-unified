#!/usr/bin/env python3
"""R-005 v2 — fix PnL using intent_id matching for release/exit attribution."""
import csv, json
from statistics import mean, stdev
from datetime import datetime, timedelta

# ── Config ──
ENTRY_Z = 2.0; RESET_Z = 0.5; ROLLING_WINDOW = 20

# ── Load ──
with open("logs/market_data/TMF_20260722_PAPER_indicators.csv") as f:
    near_rows = list(csv.DictReader(f))
with open("logs/market_data/TMF_far_20260722_PAPER.csv") as f:
    far_rows = list(csv.DictReader(f))
with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# ── Far map ──
far_map = {}
for r in far_rows:
    try:
        far_map[r["timestamp"]] = float(r["close"])
    except (ValueError, KeyError):
        pass

# ── Merge spread + Z ──
merged = []
for r in near_rows:
    ts = r.get("timestamp", "")
    if ts not in far_map: continue
    try:
        merged.append({"ts": ts, "near": float(r["close"]), "far": far_map[ts],
                       "spread": far_map[ts] - float(r["close"])})
    except (ValueError, TypeError): pass

for i, d in enumerate(merged):
    if i < ROLLING_WINDOW: d["z"] = 0.0; continue
    ws = [merged[j]["spread"] for j in range(i-ROLLING_WINDOW, i)]
    mu, s = mean(ws), stdev(ws)
    d["z"] = (d["spread"] - mu) / s if s > 0 else 0.0

# ── Episodes ──
eps = []
cur = None
for i, d in enumerate(merged):
    az = abs(d["z"])
    if cur is None:
        if az >= ENTRY_Z:
            cur = {"start_i": i, "end_i": i, "ts0": d["ts"], "ts1": d["ts"],
                   "dir": "WIDE" if d["z"] > 0 else "NARROW",
                   "entries": [], "entry_count": 0}
    else:
        cur["end_i"] = i; cur["ts1"] = d["ts"]
        if az < RESET_Z: eps.append(cur); cur = None
if cur: eps.append(cur)

# ── Group all orders by intent_id ──
by_intent = {}
for o in orders:
    iid = o.get("intent_id", "")
    if iid:
        by_intent.setdefault(iid, []).append(o)

print(f"Unique intent_ids: {len(by_intent)}")

# For each intent_id, check if it has ENTRY + RELEASE + EXIT
# Show intent_ids with all three strategies
complete = []
for iid, items in by_intent.items():
    strats = set(o.get("strategy", "") for o in items)
    if "MTS_ENTRY" in strats and "MTS_RELEASE" in strats and "MTS_EXIT" in strats:
        complete.append(iid)

print(f"Complete trades (ENTRY+RELEASE+EXIT): {len(complete)}")

# Show PnL for complete trades
def compute_pnl(intent_items):
    """Compute PnL for a complete trade from its orders."""
    entries = [o for o in intent_items if o.get("strategy") == "MTS_ENTRY"]
    releases = [o for o in intent_items if o.get("strategy") == "MTS_RELEASE"]
    exits = [o for o in intent_items if o.get("strategy") == "MTS_EXIT"]
    
    if len(entries) < 2: return None
    # Identify near (H6) vs far (I6)
    near_entry = next((e for e in entries if "H6" in str(e.get("symbol",""))), entries[0])
    far_entry = next((e for e in entries if "I6" in str(e.get("symbol",""))), entries[1])
    
    near_ep = near_entry.get("avg_fill_price") or 0
    far_ep = far_entry.get("avg_fill_price") or 0
    
    near_exit_p = None
    far_exit_p = None
    
    for o in releases + exits:
        sym = o.get("symbol", "")
        price = o.get("avg_fill_price") or 0
        if "H6" in sym:
            near_exit_p = price
        else:
            far_exit_p = price
    
    if near_exit_p is None or far_exit_p is None:
        return None
    
    # Near: sold at entry, bought at exit
    near_pnl = near_ep - near_exit_p
    # Far: bought at entry, sold at exit
    far_pnl = far_exit_p - far_ep
    return near_pnl + far_pnl

print(f"\n=== COMPLETE TRADES WITH PnL ===")
pnls = []
for iid in complete:
    items = by_intent[iid]
    pnl = compute_pnl(items)
    if pnl is not None:
        pnls.append({"iid": iid, "pnl": pnl, "items": items})
        entry = next(o for o in items if o.get("strategy") == "MTS_ENTRY")
        ts = str(entry.get("created_at", ""))[:19]
        print(f"  {ts} intent={iid[-12:]} pnl={pnl:+.0f}")

# ── Map entries to episodes ──
def parse_dt(s):
    try:
        if "T" in s: return datetime.strptime(s.split(".")[0], "%Y-%m-%dT%H:%M:%S")
        return datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
    except: return None

def find_episode(dt):
    for ep in eps:
        t0 = datetime.strptime(merged[ep["start_i"]]["ts"], "%Y-%m-%d %H:%M:%S")
        t1 = datetime.strptime(merged[ep["end_i"]]["ts"], "%Y-%m-%d %H:%M:%S")
        if t0 <= dt <= t1 + timedelta(minutes=5):
            return ep
    return None

# Map entries to episodes and assign sequence
for pnl_info in pnls:
    # Find the entries in this trade
    entries_in_trade = [o for o in pnl_info["items"] if o.get("strategy") == "MTS_ENTRY"]
    for e in entries_in_trade:
        dt = parse_dt(str(e.get("created_at", "")))
        if dt is None: continue
        ep = find_episode(dt)
        if ep is None: continue
        ep["entries"].append(e)
        ep["entry_count"] = len(ep["entries"])

# Sort entries within each episode by time
for ep in eps:
    ep["entries"].sort(key=lambda e: str(e.get("created_at", "")))

# Now assign PnL by sequence
print(f"\n{'='*60}")
print("R-005 EXPECTANCY BY ENTRY SEQUENCE")
print(f"{'='*60}")

# For each episode, assign sequence number to each entry
# Then look up the PnL from the complete trade
by_seq = {}
for ep in eps:
    if not ep["entries"]: continue
    for seq_i, e in enumerate(ep["entries"]):
        seq = seq_i + 1
        iid = e.get("intent_id", "")
        # Look up PnL for this intent
        pnl_val = None
        for pnl_info in pnls:
            if pnl_info["iid"] == iid:
                pnl_val = pnl_info["pnl"]
                break
        by_seq.setdefault(seq, []).append({
            "pnl": pnl_val,
            "ep_dir": ep["dir"],
            "ep_ts": ep["ts0"],
            "entry_ts": str(e.get("created_at", ""))[:19],
            "symbol": e.get("symbol", ""),
            "price": e.get("avg_fill_price"),
        })

for seq in sorted(by_seq.keys()):
    items = by_seq[seq]
    pnls_only = [s["pnl"] for s in items if s["pnl"] is not None]
    print(f"\nEntry #{seq}: {len(items)} total, {len(pnls_only)} with PnL")
    if pnls_only:
        print(f"  Avg: {mean(pnls_only):+.0f}  Median: {sorted(pnls_only)[len(pnls_only)//2]:+.0f}")
        print(f"  Wins: {sum(1 for p in pnls_only if p>0)}/{len(pnls_only)}")
    for s in items:
        pnl_str = f"{s['pnl']:+.0f}" if s['pnl'] is not None else "N/A"
        print(f"    [{s['ep_dir']:6s}] {s['ep_ts']} {s['entry_ts']} {s['symbol']:6s} @ {s['price']} PnL={pnl_str}")
