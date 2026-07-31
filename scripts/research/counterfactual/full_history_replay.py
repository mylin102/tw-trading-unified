#!/usr/bin/env python3
"""Combined Exit Counterfactual Replay - Full History (frozen + current)."""
import json
from collections import defaultdict

BROKER_FEE = 20.0
TAX_RATE = 2e-5
POINT_VALUE = 10.0

def cost(e, x):
    t = (e + x) * POINT_VALUE
    return 2 * BROKER_FEE + t * TAX_RATE

def pnl(e, x, long):
    p = (x - e) if long else (e - x)
    return p * POINT_VALUE - cost(e, x)

seen = set()
fills = []
for fp in ["data/frozen/parity_final/mts_trade_fills.jsonl", "logs/mts_trade_fills.jsonl"]:
    with open(fp) as f:
        for line in f:
            line = line.strip()
            if not line: continue
            fill = json.loads(line)
            key = fill["trade_id"] + "_" + fill["fill_type"] + "_" + fill["leg"]
            if key not in seen:
                seen.add(key)
                fills.append(fill)

trades = {}
for fill in fills:
    tid = fill["trade_id"]
    k = fill["fill_type"] + "_" + fill["leg"]
    if tid not in trades: trades[tid] = {}
    trades[tid][k] = fill

anchor_map = {}
for fill in fills:
    if fill["fill_type"] == "EXIT":
        a = fill.get("post_release_anchor_price")
        if a and a > 0:
            anchor_map[fill["trade_id"]] = {"leg": fill["leg"], "anchor": a}

results = []
for tid in sorted(trades.keys()):
    t = trades[tid]
    if "ENTRY_NEAR" not in t or "ENTRY_FAR" not in t: continue
    if "RELEASE_NEAR" not in t and "RELEASE_FAR" not in t: continue

    ts = t["ENTRY_NEAR"]["timestamp"]
    ne, fe = float(t["ENTRY_NEAR"]["price"]), float(t["ENTRY_FAR"]["price"])
    ns, fs = t["ENTRY_NEAR"]["side"], t["ENTRY_FAR"]["side"]

    if "RELEASE_NEAR" in t:
        rel = "NEAR"; rp = float(t["RELEASE_NEAR"]["price"])
        rel_pnl = float(t["RELEASE_NEAR"].get("realized_pnl") or 0)
        ep = float(t.get("EXIT_FAR", {}).get("realized_pnl") or 0)
        anc = anchor_map.get(tid, {}).get("anchor") if anchor_map.get(tid, {}).get("leg") == "FAR" else None
        spr = float(t["RELEASE_NEAR"].get("spread_pnl") or 0)
    else:
        rel = "FAR"; rp = float(t["RELEASE_FAR"]["price"])
        rel_pnl = float(t["RELEASE_FAR"].get("realized_pnl") or 0)
        ep = float(t.get("EXIT_NEAR", {}).get("realized_pnl") or 0)
        anc = anchor_map.get(tid, {}).get("anchor") if anchor_map.get(tid, {}).get("leg") == "NEAR" else None
        spr = float(t["RELEASE_FAR"].get("spread_pnl") or 0)

    act = rel_pnl + ep
    if anc and anc > 0:
        comb = (pnl(ne, anc, ns == "LONG") + pnl(fe, rp, fs == "LONG")) if rel == "FAR" else (pnl(ne, rp, ns == "LONG") + pnl(fe, anc, fs == "LONG"))
        diff = comb - act
    else:
        comb = None; diff = None

    h = int(ts[11:13])
    results.append({"date": ts[:10], "session": "DAY" if 5 <= h < 15 else "NIGHT",
                     "time": ts[11:19], "rel": rel, "act": act, "comb": comb, "diff": diff, "spr": spr})

# Per-date
by_date = defaultdict(list)
for r in results: by_date[r["date"]].append(r)

for d in sorted(by_date.keys()):
    rl = by_date[d]
    ha = [x for x in rl if x["comb"] is not None]
    print("\n--- {} --- {} trades, {} anchored".format(d, len(rl), len(ha)))
    print("  Time  Ses Rel    Actual  Combined     Diff")
    print("  " + ("-" * 48))
    for r in rl:
        c = "{:>+8.0f}".format(r["comb"]) if r["comb"] is not None else "     N/A"
        df = "{:>+8.0f}".format(r["diff"]) if r["diff"] is not None else "     N/A"
        print("  {} {:>4s} {:>4s} {:>+8.0f} {} {}".format(r["time"], r["session"], r["rel"], r["act"], c, df))
    print("  " + ("-" * 48))
    print("  Actual:    {:>+10.0f}".format(sum(x["act"] for x in rl)))
    if ha:
        print("  Combined:  {:>+10.0f}".format(sum(x["comb"] for x in ha)))
        print("  Improvement: {:>+8.0f}".format(sum(x["diff"] for x in ha)))

# Grand total
ha = [x for x in results if x["comb"] is not None]
far_r = [x for x in ha if x["rel"] == "FAR"]
near_r = [x for x in ha if x["rel"] == "NEAR"]
far_diff = sum(x["diff"] for x in far_r)
near_diff = sum(x["diff"] for x in near_r)
print("\n" + ("=" * 55))
print("GRAND TOTAL")
print("=" * 55)
print("  Total trades:       {}".format(len(results)))
print("  With anchor:        {}".format(len(ha)))
print("  Date range:         {} to {}".format(results[0]["date"], results[-1]["date"]))
print("")
print("  FAR-release (n={}): {}/{} BETTER, improvement={:+.0f}".format(len(far_r), sum(1 for x in far_r if x["diff"] > 0), len(far_r), far_diff))
print("  NEAR-release (n={}): {}/{} BETTER, improvement={:+.0f}".format(len(near_r), sum(1 for x in near_r if x["diff"] > 0), len(near_r), near_diff))
print("")
print("  Total Actual:       {:>+10.0f}".format(sum(x["act"] for x in results)))
print("  Total Combined:     {:>+10.0f}".format(sum(x["comb"] for x in ha)))
print("  Improvement:        {:>+10.0f}".format(far_diff + near_diff))
print("")
print("  FAR act:    {:>+10.0f}  FAR comb:   {:>+10.0f}  FAR diff: {:>+10.0f} (avg {:+.0f}/trade)".format(
    sum(x["act"] for x in far_r), sum(x["comb"] for x in far_r), far_diff, far_diff/len(far_r)))
print("  NEAR act:   {:>+10.0f}  NEAR comb:  {:>+10.0f}  NEAR diff:{:>+10.0f} (avg {:+.0f}/trade)".format(
    sum(x["act"] for x in near_r), sum(x["comb"] for x in near_r), near_diff, near_diff/len(near_r)))
