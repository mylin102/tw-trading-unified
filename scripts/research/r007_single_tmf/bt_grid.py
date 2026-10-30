"""蛛網 (grid) backtest on TX near-month 1-minute bars.

Rules (symmetric, session-anchored, fixed before results):
- anchor = first 1m bar's Close in the session
- buy levels  = anchor - k*G   (k=1..K), each fills at most once per session
- short levels= anchor + k*G   (k=1..K), each fills at most once per session
- a filled long is closed when High >= entry+G ; a filled short when Low <= entry-G
- intrabar order: opens processed BEFORE closes (conservative -- you can get
  stuck; the opposite ordering would be optimistic). Limitation, stated.
- session end: all remaining lots force-closed at last Close (full round-trip cost)
- cost = 58.9 TWD per completed round trip; 1 TMF point = 10 TWD
- max lots per side = K ; also report K<=2 since 40,000 TWD usable margin /
  18,000 per lot allows at most 2 lots.
"""
import sys, os
import numpy as np, pandas as pd

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

PV, RT = 10.0, 58.9

def load_1m(path):
    d = pd.read_parquet(path, columns=["Open", "High", "Low", "Close"])
    d = d[~d.index.duplicated()].sort_index()
    bad = d[["Open", "High", "Low"]].isna().any(axis=1)
    d = d[~bad]
    t = d.index; hm = t.hour * 60 + t.minute
    keep = ((hm >= 525) & (hm < 825)) | (hm >= 900) | (hm < 300)
    d = d[keep]
    t = d.index; hm = t.hour * 60 + t.minute
    day = t.normalize()
    sess = np.where(hm < 300, day - pd.Timedelta(days=1), day)
    kind = np.where((hm >= 525) & (hm < 825), "D", "N")
    d = d.assign(sess=pd.DatetimeIndex(sess).strftime("%Y-%m-%d") + kind)
    return d

def grid_session(hi, lo, cl, G, K):
    anchor = None
    longs, shorts = [], []
    filled_l, filled_s = set(), set()
    realized = 0.0; cycles = 0; maxlots = 0
    for i in range(len(cl)):
        if anchor is None:
            if np.isnan(cl[i]):
                continue
            anchor = cl[i]
            filled_l = set(); filled_s = set()
            continue
        h, l = hi[i], lo[i]
        if np.isnan(h) or np.isnan(l):
            continue
        # opens first (conservative)
        for k in range(1, K + 1):
            if k not in filled_l and l <= anchor - k * G:
                longs.append(anchor - k * G); filled_l.add(k)
            if k not in filled_s and h >= anchor + k * G:
                shorts.append(anchor + k * G); filled_s.add(k)
        maxlots = max(maxlots, len(longs) + len(shorts))
        # closes
        for px in list(longs):
            if h >= px + G:
                realized += (px + G - px) * PV - RT; cycles += 1; longs.remove(px)
        for px in list(shorts):
            if l <= px - G:
                realized += (px - (px - G)) * PV - RT; cycles += 1; shorts.remove(px)
    last = cl[-1]
    if not np.isnan(last):
        for px in longs:
            realized += (last - px) * PV - RT
        for px in shorts:
            realized += (px - last) * PV - RT
    return realized, cycles, maxlots, len(longs) + len(shorts)

def run(path, G, K):
    d = load_1m(path)
    rows = []
    for s, g in d.groupby("sess", sort=True):
        hi, lo, cl = g.High.to_numpy(), g.Low.to_numpy(), g.Close.to_numpy()
        net, cyc, mx, left = grid_session(hi, lo, cl, G, K)
        rows.append(dict(sess=s, net=net, cycles=cyc, maxlots=mx, forced=left))
    return pd.DataFrame(rows)

if __name__ == "__main__":
    path = sys.argv[1]
    out = []
    for K in (1, 2, 3, 5):
        for G in (5, 10, 15, 20, 30, 50):
            t = run(path, G, K)
            eq = t.net.cumsum().to_numpy()
            dd = (eq - np.maximum.accumulate(eq)).min()
            out.append(dict(K=K, G=G, sessions=len(t), cycles=int(t.cycles.sum()),
                            net=round(t.net.sum()), per_cycle=round(t.net.sum() / max(t.cycles.sum(), 1), 1),
                            win_sess_pct=round((t.net > 0).mean() * 100, 1),
                            worst_sess=round(t.net.min()), maxdd=round(dd),
                            avg_maxlots=round(t.maxlots.mean(), 2),
                            forced_pct=round(t.forced.sum() / max(t.cycles.sum() + t.forced.sum(), 1) * 100, 1)))
    df = pd.DataFrame(out)
    pd.set_option("display.width", 220); pd.set_option("display.max_rows", 100)
    print("grid configs: %d (all reported)" % len(df))
    print(df.to_string(index=False))
    df.to_csv("grid_results.csv", index=False)
