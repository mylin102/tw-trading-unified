"""tmf_vec Gate 1: signal validity of volume-confirmed Bollinger breakouts.
Event: completed 5m bar with Close > upper (LONG) or Close < lower (SHORT).
Reference: NEXT bar Open (tradeable). Also bar Close (pure signal) for h.
Outcome: direction * (Close[t+h] - ref) / ATR(t), h in 1,3,5,10,20, same session.
Uncertainty: cluster-robust SE by SESSION (events in one session are not
independent). Dedup: ignore same-direction events within 6 bars of the last
kept one (decided only from past events). Raw (no dedup) kept for comparison.
Volume: rv48 = vol / median(prev 48 bars same session-kind);
        rvtod = vol / median(same clock slot, prior 20 sessions same kind).
Cost hurdle in ATR = 7.9 pt / ATR(t)  (fees+tax 5.9 + slippage 1/side).
Splits: train <2025, valid 2025, oos1 2026H1 (TX, full OHLCV).
OOS2 2026/7-10 is close-only, no volume -> reported unconditionally only."""
import sys, os
import numpy as np, pandas as pd
import bt_bbmr as m
import bt_atrnorm as an

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")
H = (1, 3, 5, 10, 20)

def events(b, has_vol=True):
    b = b.copy()
    b["kind"] = b.sess.str[-1]; b["slot"] = b.b5.dt.strftime("%H:%M")
    if has_vol:
        v = b.Volume.astype(float)
        b["rv48"] = v / b.groupby("kind").Volume.transform(lambda s: s.shift(1).rolling(48, min_periods=24).median())
        b["rvtod"] = v / b.groupby(["kind", "slot"]).Volume.transform(lambda s: s.shift(1).rolling(20, min_periods=10).median())
    else:
        b["rv48"] = b["rvtod"] = np.nan
    O, C, sess = b.Open.to_numpy(), b.Close.to_numpy(), b.sess.to_numpy()
    rows = []; last = {1: -99, -1: -99}
    for i in range(len(b) - 1):
        if not b.warm.iat[i] or not (b.atr.iat[i] > 0) or sess[i + 1] != sess[i]:
            continue
        c = C[i]
        d = 1 if c > b.up.iat[i] else (-1 if c < b.lo.iat[i] else 0)
        if not d:
            continue
        band = b.up.iat[i] if d == 1 else b.lo.iat[i]
        a = b.atr.iat[i]
        r = dict(ts=b.b5.iat[i], sess=sess[i], dir=d, atr=a, cost=7.9 / a,
                 dist=abs(c - band) / a, rv48=b.rv48.iat[i], rvtod=b.rvtod.iat[i],
                 kind=b.kind.iat[i], kept=(i - last[d] > 6))
        if r["kept"]:
            last[d] = i
        for h in H:
            j = i + h
            ok = j < len(b) and sess[j] == sess[i]
            r[f"n{h}"] = d * (C[j] - O[i + 1]) / a if ok else np.nan   # tradeable ref
            r[f"c{h}"] = d * (C[j] - c) / a if ok else np.nan          # signal ref
        rows.append(r)
    e = pd.DataFrame(rows)
    e["split"] = e.sess.map(an.split_of)
    return e

def cluster_mean(df, col):
    x = df[["sess", col]].dropna()
    n = len(x)
    if n < 30:
        return n, np.nan, np.nan
    mu = x[col].mean()
    g = (x[col] - mu).groupby(x.sess).sum()
    se = np.sqrt((g ** 2).sum()) / n
    return n, mu, se

def table(e, cond, label):
    out = []
    for sp in ("train", "valid", "oos1", "oos2"):
        s = e[(e.split == sp) & cond(e)]
        if not len(s):
            continue
        for h in H:
            n, mu, se = cluster_mean(s, f"n{h}")
            out.append(dict(group=label, split=sp, h=h, n=n, sess=s.sess.nunique(),
                            fwd_atr=round(mu, 3), t=round(mu / se, 2) if se else np.nan,
                            cost_atr=round(s.cost.median(), 3),
                            net_atr=round(mu - s.cost.median(), 3)))
    return out

if __name__ == "__main__":
    tx = m.indicators(m.load_5m("TXFR1_5m.parquet"))
    e = events(tx)
    o2 = an.to_5m(an.load_oos2("oos2"))
    e2 = events(o2, has_vol=False)
    E = pd.concat([e, e2], ignore_index=True)
    print("events raw:", E.groupby("split").size().to_dict(),
          " dedup kept:", E[E.kept].groupby("split").size().to_dict())
    tr = E[(E.split == "train") & E.kept]
    rv_q = np.nanquantile(tr.rv48, [0.2, 0.4, 0.6, 0.8])
    d_q = np.nanquantile(tr.dist, [0.5])
    print("rv48 quintile edges (train):", np.round(rv_q, 2), " dist median:", np.round(d_q, 3))
    K = E.kept
    rows = []
    rows += table(E, lambda x: x.kept, "ALL dedup")
    rows += table(E, lambda x: x.index == x.index, "ALL raw")
    rows += table(E, lambda x: K & (x.rv48 >= rv_q[3]), "rv48 top20%")
    rows += table(E, lambda x: K & (x.rv48 < rv_q[0]), "rv48 bottom20%")
    rows += table(E, lambda x: K & (x.rvtod >= np.nanquantile(tr.rvtod, 0.8)), "rvtod top20%")
    rows += table(E, lambda x: K & (x.rv48 >= rv_q[3]) & (x.dist >= d_q[0]), "rv48 top20% & dist>=med")
    rows += table(E, lambda x: K & (x.rv48 >= rv_q[3]) & (x.kind == "D"), "rv48 top20% DAY")
    rows += table(E, lambda x: K & (x.rv48 >= rv_q[3]) & (x.kind == "N"), "rv48 top20% NIGHT")
    rows += table(E, lambda x: K & (x.rv48 >= rv_q[3]) & (x.dir == 1), "rv48 top20% LONG")
    rows += table(E, lambda x: K & (x.rv48 >= rv_q[3]) & (x.dir == -1), "rv48 top20% SHORT")
    out = pd.DataFrame(rows)
    out.to_csv("vec_gate1.csv", index=False)
    E.to_csv("vec_events.csv", index=False)
    pd.set_option("display.width", 220); pd.set_option("display.max_rows", 400)
    print("groups tested:", out.group.nunique(), "(all reported)")
    print(out.to_string(index=False))
