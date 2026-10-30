"""Decompose grid PnL: completed cycles (win side) vs forced session-end
closes (loss tail). Answers whether the high session win-rate survives."""
import sys, os
import numpy as np, pandas as pd
import bt_grid as g

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

PV, RT = g.PV, g.RT

def decomp(hi, lo, cl, G, K):
    anchor = None; longs = []; shorts = []
    filled_l, filled_s = set(), set()
    cyc_pnl = 0.0; cyc_n = 0; forced_pnl = 0.0; forced_n = 0
    for i in range(len(cl)):
        if anchor is None:
            if not np.isnan(cl[i]):
                anchor = cl[i]
            continue
        h, l = hi[i], lo[i]
        if np.isnan(h) or np.isnan(l):
            continue
        for k in range(1, K + 1):
            if k not in filled_l and l <= anchor - k * G:
                longs.append(anchor - k * G); filled_l.add(k)
            if k not in filled_s and h >= anchor + k * G:
                shorts.append(anchor + k * G); filled_s.add(k)
        for px in list(longs):
            if h >= px + G:
                cyc_pnl += G * PV - RT; cyc_n += 1; longs.remove(px)
        for px in list(shorts):
            if l <= px - G:
                cyc_pnl += G * PV - RT; cyc_n += 1; shorts.remove(px)
    last = cl[-1]
    if not np.isnan(last):
        for px in longs:
            forced_pnl += (last - px) * PV - RT; forced_n += 1
        for px in shorts:
            forced_pnl += (px - last) * PV - RT; forced_n += 1
    return cyc_pnl, cyc_n, forced_pnl, forced_n

d = g.load_1m("TXFR1_5m.parquet")
print(f"{'K':>2} {'G':>3} {'cycles':>7} {'cycle_pnl':>10} {'forced_n':>8} {'forced_pnl':>11} {'net':>9} {'cyc_win%':>9}")
for K, G in [(1,10),(1,20),(2,20),(2,15),(3,20),(5,20)]:
    cp = cn = fp = fn = 0
    for s, sub in d.groupby("sess", sort=True):
        a,b,c,e = decomp(sub.High.to_numpy(), sub.Low.to_numpy(), sub.Close.to_numpy(), G, K)
        cp += a; cn += b; fp += c; fn += e
    print(f"{K:>2} {G:>3} {cn:>7} {cp:>10.0f} {fn:>8} {fp:>11.0f} {cp+fp:>9.0f} {100.0:>8.1f}%")
print("\nnote: cyc_win% = share of COMPLETED cycles that are +%.0f (always 100 by construction)" % (G*PV-RT))
print("the loss tail lives entirely in forced session-end closes")
