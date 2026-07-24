#!/usr/bin/env python3
"""Calculate PnL for TMF trades using order log matching."""
import json, re

with open("exports/trades/TMF_20260722_orders.json") as f:
    orders = json.load(f)

# Build lookup by order_id
by_id = {o["order_id"]: o for o in orders}

# Extract PnL from order log patterns
# Entry: near=SELL@price far=BUY@price
# Release: RELEASE_NEAR or RELEASE_FAR filled
# Exit: EXIT_REMAINING filled

# Parse entry pairs from the name/symbol pattern
# Group by intent_id
groups = {}
for o in orders:
    iid = o.get("intent_id", "")
    groups.setdefault(iid, []).append(o)

print("=== TRADES BY INTENT ===")
for iid, items in sorted(groups.items()):
    strat = items[0].get("strategy", "?")
    if strat != "MTS_ENTRY":
        continue
    near = [i for i in items if "H6" in i.get("symbol", "")]
    far = [i for i in items if "I6" in i.get("symbol", "")]
    np = near[0].get("avg_fill_price") if near else 0
    fp = far[0].get("avg_fill_price") if far else 0
    ns = near[0].get("side", "?") if near else "?"
    fs = far[0].get("side", "?") if far else "?"
    ts = str(items[0].get("created_at", ""))[:19]
    
    # Find matching release and exit by following the order_id/history
    # release: the leg that hits stop
    # exit: the remaining leg
    print(f"{ts} {ns} near@{np} {fs} far@{fp} intent={iid[-12:]}")

# Now manually calculate PnL from the chronological log
print("\n=== PnL BY ORDER CHAIN ===")
# From the out log, entries and their releases:
pairs = [
    # (entry_near_price, entry_far_price, release_type, release_price, exit_price)
    # Night trades
    (44628, 44839, "NEAR_RELEASE", 44602, 44800),  # near released at 44602, far exit at 44800
    (44666, 44874, "FAR_RELEASE", 44803, 44594),   # far released at 44803, near exit at 44594
    (44602, 44800, "FAR_RELEASE", 44803, 44594),
    # Wait, this doesn't match. Let me re-trace from the log.
]

# Actually let me just trace from the order IDs
# From the log, order_id sequence and prices
order_chain = [
    # (entry_near_price, entry_far_price, release_leg, release_price, exit_price)
    # Night session
    (44628, 44839, "NEAR", 44602, 44800),    # orders 003-004 → 001-002
    (44666, 44874, "NEAR", 44690, 44900),    # 007-008 → 005-006
    (44602, 44800, "FAR", 44803, 44594),     # 011-012 → 009-010
    (44790, 44996, "NEAR", 44773, 45000),    # 015-016 → 013-014  
    (44942, 45143, "NEAR", 44897, 45145),    # 019-020 → 017-018
    # Day session
    (45123, 45332, "NEAR", 45199, 45373),    # 023-024 → 025-026
    (45288, 45500, "FAR", 45427, 45285),     # 027-028 → 029-030
    (45342, 45544, "FAR", 45459, 45295),     # 031-032 → 033-034
    (45074, 45275, "NEAR", 45163, 45337),    # 035-036 → 037-038
    (45158, 45353, "FAR", 45234, 45145),     # 039-040 → 041-042
    (44979, 45186, "FAR", 45160, 44968),     # 045-046 → 043-044
    (44938, 45149, "NEAR", 45085, 45265),    # 049-050 → 047-048
    (44908, 45117, "FAR", 45031, 44941),     # 053-054 → 051-052
    (44998, 45210, "NEAR", 45035, 45170),    # 057-058 → 055-056
    (44864, 45080, "FAR", 45045, 44889),     # 061-062 → 059-060
    (45010, 45225, "NEAR", 45038, 45223),    # 065-066 → 063-064
]

total_pnl = 0
wins = 0
losses = 0
for ep_near, ep_far, rel_leg, rel_price, ex_price in order_chain:
    if rel_leg == "NEAR":
        # Near released: pnl = (entry_near - release_near) + (exit_far - entry_far)
        # Near was sold, released (bought back)
        near_pnl = ep_near - rel_price
        # Far was bought, exit (sold)
        far_pnl = ex_price - ep_far
    else:
        # Far released: pnl = (exit_near - entry_near) + (release_far - entry_far)
        # Far was bought, released (sold)
        far_pnl = rel_price - ep_far
        # Near was sold, exit (bought back)
        near_pnl = ep_near - ex_price
    pnl = near_pnl + far_pnl
    total_pnl += pnl
    if pnl > 0:
        wins += 1
    else:
        losses += 1
    spread_ep = ep_far - ep_near
    spread_ex = ex_price - rel_price if rel_leg == "NEAR" else rel_price - ex_price
    print(f"  entry=({ep_near:.0f},{ep_far:.0f}) spread@entry={spread_ep:.0f} release={rel_leg}@{rel_price:.0f} exit@{ex_price:.0f} near_pnl={near_pnl:+.0f} far_pnl={far_pnl:+.0f} total={pnl:+.0f}")

print(f"\nTotal PnL: {total_pnl:.0f}")
print(f"Wins: {wins}, Losses: {losses}, WinRate: {wins/(wins+losses)*100:.0f}%")
