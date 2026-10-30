"""Volume filter at Bollinger band touches.
Event: completed 5m bar touches a band (High>=up -> 'upper', Low<=lo -> 'lower').
Fade direction: upper -> short, lower -> long.
Outcome: forward return from NEXT bar Open to Close of bar t+h (h=3,6,12),
signed in the FADE direction, in ATR units (ATR of the event bar) -- scale
invariant across 2023 (ATR~8pt) vs 2026 (ATR~63pt). Same session only.
Cost in ATR = 7.9pt / ATR(event).  net_fade = fwd - cost ; net_follow = -fwd - cost.
Volume features (known at event bar close):
  vr48 : vol / median(vol, prev 48 bars of the SAME session kind D/N)
  vtod : vol / median(vol at the same 5m clock slot over prior 20 sessions of same kind)
Quintile edges fixed on TRAIN. Report mean + SE per split."""
import sys, os
import numpy as np, pandas as pd
import bt_bbmr as m

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

b = m.indicators(m.load_5m("TXFR1_5m.parquet"))
b["kind"] = b.sess.str[-1]
b["slot"] = b.b5.dt.strftime("%H:%M")
v = b.Volume.astype(float)
b["vr48"] = v / b.groupby("kind").Volume.transform(lambda s: s.shift(1).rolling(48, min_periods=24).median())
b["vtod"] = v / b.groupby(["kind", "slot"]).Volume.transform(lambda s: s.shift(1).rolling(20, min_periods=10).median())

O, C = b.Open.to_numpy(), b.Close.to_numpy()
sess = b.sess.to_numpy(); n = len(b)
ev = []
for i in range(n - 13):
    if not b.warm.iat[i] or np.isnan(b.atr.iat[i]) or b.atr.iat[i] <= 0:
        continue
    side = 0
    if b.High.iat[i] >= b.up.iat[i]: side = -1      # fade upper = short
    elif b.Low.iat[i] <= b.lo.iat[i]: side = 1      # fade lower = long
    if not side:
        continue
    a = b.atr.iat[i]; row = dict(ts=b.b5.iat[i], side=side, atr=a, cost=7.9 / a,
                                 vr48=b.vr48.iat[i], vtod=b.vtod.iat[i])
    for h in (3, 6, 12):
        j = i + h
        row[f"f{h}"] = (side * (C[j] - O[i + 1]) / a) if sess[j] == sess[i] else np.nan
    ev.append(row)
e = pd.DataFrame(ev)
e["split"] = np.select([e.ts < "2025-01-01", e.ts < "2026-01-01"], ["train", "valid"], "oos")
print("touch events:", len(e), e.split.value_counts().to_dict())
print("median cost in ATR by split:", e.groupby("split").cost.median().round(3).to_dict())

rows = []
for f in ("vr48", "vtod"):
    tr = e[e.split == "train"]
    edges = np.nanquantile(tr[f], [0.2, 0.4, 0.6, 0.8])
    q = np.digitize(e[f], edges)
    for k in range(5):
        for h in (3, 6, 12):
            r = {"feat": f, "q": k + 1, "edge_hi": round(edges[k], 2) if k < 4 else None, "h": h}
            for sp in ("train", "valid", "oos"):
                s = e[(q == k) & (e.split == sp) & e[f].notna()][f"f{h}"].dropna()
                c = e[(q == k) & (e.split == sp) & e[f].notna()].cost.median()
                r[f"{sp}_n"] = len(s)
                r[f"{sp}_fwd"] = round(s.mean(), 3) if len(s) else None
                r[f"{sp}_se"] = round(s.std(ddof=1) / np.sqrt(len(s)), 3) if len(s) > 1 else None
                r[f"{sp}_cost"] = round(c, 3) if len(s) else None
            rows.append(r)
out = pd.DataFrame(rows)
out.to_csv("volume_filter_results.csv", index=False)
pd.set_option("display.width", 260); pd.set_option("display.max_rows", 200)
print("\nfwd = mean forward return in FADE direction (ATR units). >0 favours fade, <0 favours follow.")
print("net_fade = fwd - cost ; net_follow = -fwd - cost")
print(out.to_string(index=False))
