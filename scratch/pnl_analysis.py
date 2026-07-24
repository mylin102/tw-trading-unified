#!/usr/bin/env python3
"""Calculate PnL per trade for TMF 2026-07-22."""
import json
from collections import defaultdict

with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# Chronological order
orders.sort(key=lambda o: str(o.get("created_at", "")))

# Track each trade lifecycle
# Strategy: ENTER sell near + buy far, RELEASE one leg, EXIT remaining leg
trades = []

for i, o in enumerate(orders):
    strat = o.get("strategy", "")
    if strat == "MTS_ENTRY":
        sym = o.get("symbol", "")
        side = o.get("side", "")
        price = o.get("avg_fill_price")
        ts = str(o.get("created_at", ""))[:19]
        # Pair entries by proximity (within 2s)
        for j in range(i+1, min(i+3, len(orders))):
            o2 = orders[j]
            if o2.get("strategy") == "MTS_ENTRY":
                sym2 = o2.get("symbol", "")
                side2 = o2.get("side", "")
                price2 = o2.get("avg_fill_price")
                ts2 = str(o2.get("created_at", ""))[:19]
                trades.append({
                    "ts": ts, "entry_near_sym": sym, "entry_near_side": side, "entry_near_price": price,
                    "entry_far_sym": sym2, "entry_far_side": side2, "entry_far_price": price2,
                    "entry_ts2": ts2,
                })
                break

# Now fill in RELEASE and EXIT for each trade
# Match by looking for the next RELEASE and then EXIT that happens after entry
for t in trades:
    entry_ts = t["ts"]
    # Find first RELEASE after entry
    for o in orders:
        ts = str(o.get("created_at", ""))[:19]
        if ts < entry_ts:
            continue
        if o.get("strategy") == "MTS_RELEASE" and o.get("avg_fill_price"):
            # Check if this closes the near or far leg
            sym = o.get("symbol", "")
            side = o.get("side", "")
            price = o.get("avg_fill_price")
            # NEAR leg was sold, so RELEASE buys it back
            # FAR leg was bought, so RELEASE sells it
            if sym == t["entry_near_sym"]:
                t["release_near_side"] = side
                t["release_near_price"] = price
                t["release_ts"] = str(o.get("created_at", ""))[:19]
            elif sym == t["entry_far_sym"]:
                t["release_far_side"] = side
                t["release_far_price"] = price
                t["release_ts"] = str(o.get("created_at", ""))[:19]
            break
    
    # Find first EXIT after entry
    for o in orders:
        ts = str(o.get("created_at", ""))[:19]
        if ts < entry_ts:
            continue
        if o.get("strategy") == "MTS_EXIT" and o.get("avg_fill_price"):
            sym = o.get("symbol", "")
            side = o.get("side", "")
            price = o.get("avg_fill_price")
            # If far was released, exit is near (buy back)
            # If near was released, exit is far (sell)
            if "release_far_price" in t and sym == t["entry_near_sym"]:
                t["exit_side"] = side
                t["exit_price"] = price
                t["exit_ts"] = str(o.get("created_at", ""))[:19]
            elif "release_near_price" in t and sym == t["entry_far_sym"]:
                t["exit_side"] = side
                t["exit_price"] = price
                t["exit_ts"] = str(o.get("created_at", ""))[:19]
            elif "release_far_price" not in t and "release_near_price" not in t:
                continue
            break

# Calculate PnL
print("=== TRADE PnL ANALYSIS ===")
print(f"{'Time':<14} {'Entry(Near)':<20} {'Entry(Far)':<20} {'Release':<20} {'Exit':<20} {'PnL':>8}")
print("-" * 102)

total_pnl = 0
win_count = 0
loss_count = 0

for t in trades:
    near_ep = t.get("entry_near_price") or 0
    far_ep = t.get("entry_far_price") or 0
    rp = t.get("release_near_price") or t.get("release_far_price") or 0
    ep = t.get("exit_price") or 0
    
    # spread PnL
    # Near was sold, so pnl = (sell_price - buy_price) * 1
    # Far was bought, so pnl = (sell_price - buy_price) * 1
    
    if "release_near_price" in t:
        # Near leg released (bought back)
        near_pnl = near_ep - t["release_near_price"]
        # Far leg exits (sold)
        far_pnl = t["exit_price"] - far_ep
    elif "release_far_price" in t:
        # Far leg released (sold)
        far_pnl = t["release_far_price"] - far_ep
        # Near leg exits (bought back)
        near_pnl = near_ep - t["exit_price"]
    else:
        near_pnl = 0
        far_pnl = 0
    
    pnl = near_pnl + far_pnl
    total_pnl += pnl
    if pnl > 0:
        win_count += 1
    else:
        loss_count += 1
    
    near_str = f"{t.get('entry_near_side','?')} {t['entry_near_sym']} @ {near_ep}"
    far_str = f"{t.get('entry_far_side','?')} {t['entry_far_sym']} @ {far_ep}"
    
    rel_str = ""
    if "release_near_price" in t:
        rel_str = f"Buy near @ {t['release_near_price']}"
    elif "release_far_price" in t:
        rel_str = f"Sell far @ {t['release_far_price']}"
    
    ext_str = f"{t.get('exit_side','?')} @ {ep}" if t.get("exit_price") else "--"
    
    print(f"{t['ts']:<14} {near_str:<20} {far_str:<20} {rel_str:<20} {ext_str:<20} {pnl:>8.0f}")

print("-" * 102)
print(f"{'TOTAL':<14} {'':<20} {'':<20} {'':<20} {'':<20} {total_pnl:>8.0f}")
print(f"Wins: {win_count}, Losses: {loss_count}, WinRate: {win_count/(win_count+loss_count)*100:.0f}%" if (win_count+loss_count) > 0 else "")
print(f"Avg PnL: {total_pnl/(win_count+loss_count):.0f}" if (win_count+loss_count) > 0 else "")
