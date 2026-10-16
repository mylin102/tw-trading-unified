#!/usr/bin/env python3
"""Combined Exit Counterfactual Replay — Full History."""
import json
from collections import defaultdict

BROKER_FEE = 20.0
TAX_RATE = 2e-5
POINT_VALUE = 10.0

def cost(entry, exit_px):
    turnover = (entry + exit_px) * POINT_VALUE
    return 2 * BROKER_FEE + turnover * TAX_RATE

def leg_pnl(entry, exit_px, is_long):
    pts = (exit_px - entry) if is_long else (entry - exit_px)
    return pts * POINT_VALUE - cost(entry, exit_px)

fills = []
with open("logs/mts_trade_fills.jsonl") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        fills.append(json.loads(line))

trades = {}
for fill in fills:
    tid = fill["trade_id"]
    ft = fill["fill_type"]
    leg = fill["leg"]
    key = "{}_{}".format(ft, leg)
    if tid not in trades:
        trades[tid] = {}
    trades[tid][key] = fill

anchor_map = {}
for fill in fills:
    if fill["fill_type"] == "EXIT":
        tid = fill["trade_id"]
        anchor = fill.get("post_release_anchor_price")
        if anchor is not None and anchor > 0:
            anchor_map[tid] = {
                "remaining_leg": fill["leg"],
                "anchor_price": anchor,
            }

by_date = defaultdict(list)

for tid in sorted(trades.keys()):
    t = trades[tid]
    if "ENTRY_NEAR" not in t or "ENTRY_FAR" not in t:
        continue
    if "RELEASE_NEAR" not in t and "RELEASE_FAR" not in t:
        continue

    ts_full = t["ENTRY_NEAR"]["timestamp"]
    trade_date = ts_full[:10]

    ne = float(t["ENTRY_NEAR"]["price"])
    fe = float(t["ENTRY_FAR"]["price"])
    ns = t["ENTRY_NEAR"]["side"]
    fs = t["ENTRY_FAR"]["side"]
    ts = ts_full[11:19]

    if "RELEASE_NEAR" in t:
        released_leg = "NEAR"
        release_price = float(t["RELEASE_NEAR"]["price"])
        release_pnl = float(t["RELEASE_NEAR"].get("realized_pnl") or 0)
        exit_pnl = float(t.get("EXIT_FAR", {}).get("realized_pnl") or 0)
        ainfo = anchor_map.get(tid, {})
        anchor = ainfo.get("anchor_price") if ainfo.get("remaining_leg") == "FAR" else None
    elif "RELEASE_FAR" in t:
        released_leg = "FAR"
        release_price = float(t["RELEASE_FAR"]["price"])
        release_pnl = float(t["RELEASE_FAR"].get("realized_pnl") or 0)
        exit_pnl = float(t.get("EXIT_NEAR", {}).get("realized_pnl") or 0)
        ainfo = anchor_map.get(tid, {})
        anchor = ainfo.get("anchor_price") if ainfo.get("remaining_leg") == "NEAR" else None
    else:
        continue

    actual_net = release_pnl + exit_pnl

    if anchor is not None and anchor > 0:
        if released_leg == "FAR":
            combined = leg_pnl(ne, anchor, ns == "LONG") + leg_pnl(fe, release_price, fs == "LONG")
        else:
            combined = leg_pnl(ne, release_price, ns == "LONG") + leg_pnl(fe, anchor, fs == "LONG")
        diff = combined - actual_net
    else:
        combined = None
        diff = None

    by_date[trade_date].append({
        "ts": ts, "ne": ne, "fe": fe,
        "actual": actual_net, "combined": combined,
        "diff": diff, "rel": released_leg, "tid": tid[-12:]
    })

def print_date_summary(label, trades_list):
    if not trades_list:
        print("\n--- {} --- (no trades)".format(label))
        return
    total_actual = sum(t["actual"] for t in trades_list)
    has_comb = [t for t in trades_list if t["combined"] is not None]
    total_combined = sum(t["combined"] for t in has_comb)
    total_diff = sum(t["diff"] for t in has_comb)
    actual_wins = sum(1 for t in trades_list if t["actual"] > 0)
    comb_wins = sum(1 for t in has_comb if t["combined"] > 0)

    print("\n--- {} --- {} trades, {} anchored".format(label, len(trades_list), len(has_comb)))
    print("  Time    Rel     Actual   Combined      Diff")
    print("  " + ("-" * 48))
    for t in trades_list:
        if t["combined"] is not None:
            c = "{:>+10.1f}".format(t["combined"])
            d = "{:>+10.1f}".format(t["diff"])
        else:
            c = "       N/A"
            d = "       N/A"
        a = "{:>+10.1f}".format(t["actual"])
        print("  {} {:>4s} {} {} {}  {}".format(
            t["ts"], t["rel"], a, c, d, t["tid"]))
    print("  " + ("-" * 48))
    print("  Actual:       {:>+10.1f}".format(total_actual))
    print("  Combined:     {:>+10.1f}".format(total_combined))
    print("  Improvement:  {:>+10.1f}".format(total_diff))
    print("  Actual WR:   {}/{} ({:.0f}%)".format(actual_wins, len(trades_list),
          100*actual_wins/len(trades_list)))
    print("  Combined WR: {}/{} ({:.0f}%)".format(comb_wins, len(has_comb),
          100*comb_wins/len(has_comb)))

# Grand totals
grand_actual = 0
grand_combined = 0
grand_diff = 0
grand_total = 0
grand_anchored = 0

for d in sorted(by_date.keys()):
    trades_list = by_date[d]
    # Filter trades that have anchor data for combined calculation
    print_date_summary(d, trades_list)
    
    grand_actual += sum(t["actual"] for t in trades_list)
    has_comb = [t for t in trades_list if t["combined"] is not None]
    grand_combined += sum(t["combined"] for t in has_comb)
    grand_diff += sum(t["diff"] for t in has_comb)
    grand_total += len(trades_list)
    grand_anchored += len(has_comb)

print("\n" + ("=" * 55))
print("GRAND TOTAL (all dates)")
print("=" * 55)
print("  Total trades:        {}".format(grand_total))
print("  With anchor:         {}".format(grand_anchored))
print("  Actual total:        {:>+10.1f}".format(grand_actual))
print("  Combined total:      {:>+10.1f}".format(grand_combined))
print("  Improvement:         {:>+10.1f}".format(grand_diff))
print("  Avg improvement/trade: {:>+7.1f}".format(grand_diff / grand_anchored if grand_anchored else 0))
