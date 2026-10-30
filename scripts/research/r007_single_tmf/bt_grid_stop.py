"""Grid + stop-loss. Same grid as bt_grid.py (session anchor, K levels each
side, take profit at +G, conservative open-before-close), plus a STOP:
if price moves S grids beyond the outermost level (anchor -/+ (K+S)*G),
all open lots on that side are closed at the stop price (gap -> bar Open),
and that side is disabled for the rest of the session (no re-entry --
re-arming would just repeat the loss). Session end still force-closes.
Reported per split (train/valid/oos) so nothing is chosen on OOS."""
import sys, os, itertools
import numpy as np, pandas as pd
import bt_grid as g

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

PV, RT = g.PV, g.RT

def session(op, hi, lo, cl, G, K, S):
    anchor = None; longs = []; shorts = []
    fl, fs = set(), set(); off_l = off_s = False
    tp = sl = fc = 0.0; n_tp = n_sl = n_fc = 0
    for i in range(len(cl)):
        if anchor is None:
            anchor = cl[i]; continue
        o, h, l = op[i], hi[i], lo[i]
        stop_l = anchor - (K + S) * G; stop_s = anchor + (K + S) * G
        for k in range(1, K + 1):
            if not off_l and k not in fl and l <= anchor - k * G:
                longs.append(anchor - k * G); fl.add(k)
            if not off_s and k not in fs and h >= anchor + k * G:
                shorts.append(anchor + k * G); fs.add(k)
        if longs and l <= stop_l:
            px = min(stop_l, o)
            for e in longs:
                sl += (px - e) * PV - RT; n_sl += 1
            longs = []; off_l = True
        if shorts and h >= stop_s:
            px = max(stop_s, o)
            for e in shorts:
                sl += (e - px) * PV - RT; n_sl += 1
            shorts = []; off_s = True
        for e in list(longs):
            if h >= e + G:
                tp += G * PV - RT; n_tp += 1; longs.remove(e)
        for e in list(shorts):
            if l <= e - G:
                tp += G * PV - RT; n_tp += 1; shorts.remove(e)
    last = cl[-1]
    for e in longs:
        fc += (last - e) * PV - RT; n_fc += 1
    for e in shorts:
        fc += (e - last) * PV - RT; n_fc += 1
    return tp, sl, fc, n_tp, n_sl, n_fc

d = g.load_1m("TXFR1_5m.parquet")
sessions = [(s, x.Open.to_numpy(), x.High.to_numpy(), x.Low.to_numpy(), x.Close.to_numpy())
            for s, x in d.groupby("sess", sort=True)]
def split_of(s):
    return "train" if s < "2025-01-01" else ("valid" if s < "2026-01-01" else "oos")

rows = []
for K, G, S in itertools.product((1, 2), (10, 20, 30, 50), (1, 2, 4, None)):
    agg = {}
    for s, op, hi, lo, cl in sessions:
        Sx = 10_000 if S is None else S
        r = session(op, hi, lo, cl, G, K, Sx)
        a = agg.setdefault(split_of(s), np.zeros(7))
        a[:6] += r; a[6] += 1
    for sp in ("train", "valid", "oos"):
        tp, sl, fc, ntp, nsl, nfc, nsess = agg[sp]
        rows.append(dict(K=K, G=G, stop=("none" if S is None else f"{S}G"), split=sp,
                         sess=int(nsess), tp_n=int(ntp), tp=round(tp), stop_n=int(nsl), stop_pnl=round(sl),
                         forced_n=int(nfc), forced=round(fc), net=round(tp + sl + fc),
                         net_per_sess=round((tp + sl + fc) / nsess, 1)))
out = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 300)
print("configs:", len(out) // 3, "(all reported; K<=2 = margin-feasible)")
print(out.to_string(index=False))
out.to_csv("grid_stop_results.csv", index=False)
