"""Multi-timeframe regime test: 5m BB mean-reversion signals (P2, slip=0),
regime measured on HIGHER timeframe (15m, 60m) bars that were already
COMPLETED when the 5m signal bar closed (merge_asof on completion time --
no look-ahead). Quintile edges fixed on TRAIN only. Hurdle ~7.9 pts/trade."""
import sys, os
import numpy as np, pandas as pd
import bt_bbmr as m

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

base = m.load_5m("TXFR1_5m.parquet")
b5 = m.indicators(base.copy())

def higher(minutes):
    x = base.copy()
    x["bh"] = x.b5.dt.floor(f"{minutes}min")
    h = x.groupby(["sess", "bh"], sort=True).agg(Open=("Open", "first"), High=("High", "max"),
            Low=("Low", "min"), Close=("Close", "last"), Volume=("Volume", "sum"),
            last5=("b5", "max")).reset_index()
    h = h.rename(columns={"bh": "b5"}).sort_values("b5").reset_index(drop=True)
    h = m.indicators(h)
    r = h.Close.diff()
    h["er"] = (h.Close - h.Close.shift(12)).abs() / r.abs().rolling(12).sum()
    # vectorized rolling lag-1 autocorrelation over 32 bars
    a, bb = r, r.shift(1)
    w = 32
    cov = (a * bb).rolling(w).mean() - a.rolling(w).mean() * bb.rolling(w).mean()
    h["ac"] = cov / (a.rolling(w).std(ddof=0) * bb.rolling(w).std(ddof=0))
    h["abs_slope"] = h.slope.abs()
    # completion time = start of the last 5m bar in the bucket + 5min
    # (a partial session-end bucket completes when its last 5m bar does)
    h["done"] = h.last5 + pd.Timedelta(minutes=5)
    keep = ["done", "adx", "er", "ac", "abs_slope", "bwr"]
    return h[keep].rename(columns={c: f"{c}_{minutes}m" for c in keep if c != "done"})

t = m.run(b5, mode="P2", slip=0.0)
t["pts"] = t.gross / m.PV
t["sig_done"] = b5.b5.to_numpy()[t.sig_i.to_numpy()] + pd.Timedelta(minutes=5)
t = t.sort_values("sig_done")
for mins in (15, 60):
    h = higher(mins).sort_values("done")
    t = pd.merge_asof(t, h, left_on="sig_done", right_on="done", direction="backward",
                      allow_exact_matches=True).drop(columns=["done"])
t["split"] = np.select([t.entry_ts < "2025-01-01", t.entry_ts < "2026-01-01"], ["train", "valid"], "oos")

feats = [c for c in t.columns if c.endswith(("_15m", "_60m"))]
rows = []
tr = t[t.split == "train"]
for f in feats:
    edges = np.nanquantile(tr[f], [0.2, 0.4, 0.6, 0.8])
    q = np.digitize(t[f], edges)
    for k in range(5):
        row = {"feature": f, "q": k + 1}
        for sp in ("train", "valid", "oos"):
            s = t[(q == k) & (t.split == sp) & t[f].notna()]
            row[f"{sp}_n"] = len(s)
            row[f"{sp}_pts"] = round(s.pts.mean(), 2) if len(s) else None
            row[f"{sp}_se"] = round(s.pts.std(ddof=1) / np.sqrt(len(s)), 2) if len(s) > 1 else None
        rows.append(row)
out = pd.DataFrame(rows)
pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
print("P2 5m trades:", len(t), " baseline pts:", {sp: round(t[t.split == sp].pts.mean(), 2) for sp in ("train", "valid", "oos")})
print(out.to_string(index=False))
out.to_csv("mtf_regime_buckets.csv", index=False)
