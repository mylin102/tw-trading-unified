"""Can a REAL-TIME range detector select the BB mean-reversion trades that
have edge? For every P2 trade (slip=0, gross before fees), attach regime
features known at the signal bar, bucket by quintile (edges fixed on TRAIN
only), and report gross pts/trade per bucket in train / valid / oos.
Hurdle to beat: ~7.9 pts/trade (5.9 fees+tax + 2 pts slippage).
An ex-post ORACLE (future efficiency ratio, NOT knowable in real time) is
included only as a ceiling -- it is never a deployable rule."""
import sys, os
import numpy as np, pandas as pd
import bt_bbmr as m

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

b = m.indicators(m.load_5m("TXFR1_5m.parquet"))
C = b.Close
r = C.diff()
# real-time features (only data up to and including bar i)
b["er20"] = (C - C.shift(20)).abs() / r.abs().rolling(20).sum()            # Kaufman efficiency ratio
b["ac60"] = r.rolling(60).apply(lambda x: pd.Series(x).autocorr(1), raw=True)  # lag-1 autocorr
b["atr_pct"] = b.atr.rolling(500).rank(pct=True)
b["bw_pct"] = ((b.up - b.lo) / b.mid).rolling(500).rank(pct=True)
b["abs_slope"] = b.slope.abs()
hi48, lo48 = b.High.rolling(48).max(), b.Low.rolling(48).min()
b["range48_atr"] = (hi48 - lo48) / b.atr                                    # 4h box height in ATRs
# ex-post oracle (FUTURE 12 bars) -- ceiling only
b["fut_er12"] = ((C.shift(-12) - C).abs() / r.abs()[::-1].rolling(12).sum()[::-1].shift(-1))

t = m.run(b, mode="P2", slip=0.0)
t["pts"] = t.gross / m.PV
feats = ["adx", "abs_slope", "bwr", "er20", "ac60", "atr_pct", "bw_pct", "range48_atr", "fut_er12"]
for f in feats:
    t[f] = b[f].to_numpy()[t.sig_i.to_numpy()]
t["split"] = np.select([t.entry_ts < "2025-01-01", t.entry_ts < "2026-01-01"], ["train", "valid"], "oos")

rows = []
tr = t[t.split == "train"]
for f in feats:
    edges = np.nanquantile(tr[f], [0.2, 0.4, 0.6, 0.8])   # fixed on TRAIN only
    q = np.digitize(t[f], edges)
    for k in range(5):
        row = {"feature": f, "quintile": k + 1}
        for sp in ("train", "valid", "oos"):
            s = t[(q == k) & (t.split == sp)]
            row[f"{sp}_n"] = len(s)
            row[f"{sp}_pts"] = round(s.pts.mean(), 2) if len(s) else None
        rows.append(row)
out = pd.DataFrame(rows)
pd.set_option("display.width", 220); pd.set_option("display.max_rows", 100)
print(f"P2 trades: {len(t)}  (gross pts/trade, slip=0, before 5.9pt fees)")
print("baseline all:", {sp: round(t[t.split == sp].pts.mean(), 2) for sp in ("train", "valid", "oos")})
print(out.to_string(index=False))
out.to_csv("regime_buckets.csv", index=False)
