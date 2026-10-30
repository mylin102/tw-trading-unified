"""ATR-normalised re-evaluation + second out-of-sample (2026-07..10, TMF).

Data sets (all as 1m OHLC, session-tagged like bt_grid.load_1m):
  TX      : TX near continuous 1m, true OHLC, 2023-04..2026-06
  TX_C    : same TX, but O=H=L=C=close  (close-only CONTROL -> measures the
            bias of having no intrabar high/low, which is all OOS2 has)
  OOS2    : TMF near, 1m CLOSE ONLY, 2026-07..2026-10 (Sept missing),
            from tmf_calendar_spread_*.csv Close_near (dedup by ts)

Strategies, all parameters in ATR units (5m Wilder ATR14, last COMPLETED bar):
  BB  : P2 close-back-inside entry, mid-band exit, stop 2*ATR, max 24 bars
        (bt_bbmr.run, slip 1pt/side, cost 58.9 TWD) -- already ATR-scaled
  GRID: session anchor; grid step G = g*ATR(anchor), K levels/side,
        TP +G, no TP on entry bar, opens before closes,
        stop: 'none' | 'beyondNG' (anchor -/+ (K+2)*G) ; side off after stop
        cost 58.9 TWD/RT, zero slippage (limit).  K<=2 (margin).
Outputs per split: train / valid / oos1 (2026H1) / oos2 (2026-07..10).
"""
import sys, os, glob, itertools
import numpy as np, pandas as pd
import bt_bbmr as m
import bt_grid as g

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")
PV, RT = 10.0, 58.9

def tag_sessions(d):
    t = d.index; hm = t.hour * 60 + t.minute
    keep = ((hm >= 525) & (hm < 825)) | (hm >= 900) | (hm < 300)
    d = d[keep]
    t = d.index; hm = t.hour * 60 + t.minute
    day = t.normalize()
    sess = np.where(hm < 300, day - pd.Timedelta(days=1), day)
    kind = np.where((hm >= 525) & (hm < 825), "D", "N")
    return d.assign(sess=pd.DatetimeIndex(sess).strftime("%Y-%m-%d") + kind)

def load_oos2(folder):
    parts = []
    for f in sorted(glob.glob(os.path.join(folder, "tmf_calendar_spread_*.csv"))):
        x = pd.read_csv(f, usecols=["ts", "Close_near"])
        parts.append(x)
    x = pd.concat(parts).dropna()
    x["ts"] = pd.to_datetime(x.ts)
    x = x.drop_duplicates("ts", keep="last").set_index("ts").sort_index()
    x = x[x.index >= "2026-07-01"]
    c = x.Close_near.astype(float)
    d = pd.DataFrame({"Open": c, "High": c, "Low": c, "Close": c, "Volume": np.nan})
    return tag_sessions(d)

def to_5m(one):
    x = one.copy(); x["b5"] = x.index.floor("5min")
    if "Volume" not in x.columns:
        x["Volume"] = np.nan
    b = x.groupby(["sess", "b5"], sort=True).agg(Open=("Open", "first"), High=("High", "max"),
            Low=("Low", "min"), Close=("Close", "last"), Volume=("Volume", "sum")).reset_index()
    return m.indicators(b.sort_values("b5").reset_index(drop=True))

def split_of(s):
    if s < "2025-01-01": return "train"
    if s < "2026-01-01": return "valid"
    if s < "2026-07-01": return "oos1"
    return "oos2"

def bb_eval(one, label):
    b5 = to_5m(one)
    t = m.run(b5, mode="P2", stop_k=2.0, max_hold=24, slip=1.0)
    t["atr"] = b5.atr.to_numpy()[t.sig_i.to_numpy()]
    t["split"] = t.sess.map(split_of)
    rows = []
    for sp, s in t.groupby("split"):
        rows.append(dict(data=label, strat="BB_P2", split=sp, n=len(s), net=round(s.net.sum()),
                         gross_atr=round((s.gross / PV / s.atr).mean(), 3),
                         net_atr=round((s.net / PV / s.atr).mean(), 3),
                         se_atr=round((s.net / PV / s.atr).std(ddof=1) / np.sqrt(len(s)), 3)))
    return rows

def grid_eval(one, label, configs):
    b5 = to_5m(one)
    b5["done"] = b5.b5 + pd.Timedelta(minutes=5)
    x = one.reset_index().rename(columns={one.index.name or "index": "ts"})
    x["t_end"] = x.ts + pd.Timedelta(minutes=1)
    x = pd.merge_asof(x.sort_values("t_end"), b5[["done", "sess", "atr", "warm"]].rename(columns={"sess": "bs"}),
                      left_on="t_end", right_on="done", direction="backward")
    x.loc[(x.bs != x.sess) | ~x.warm.fillna(False).astype(bool), "atr"] = np.nan
    groups = [(s, z.High.to_numpy(), z.Low.to_numpy(), z.Close.to_numpy(), z.atr.to_numpy())
              for s, z in x.groupby("sess", sort=True)]
    rows = []
    for K, gm, stop in configs:
        agg = {}
        for s, hi, lo, cl, atr in groups:
            ok = np.where(~np.isnan(atr))[0]
            if len(ok) < 2:
                continue
            a0 = ok[0]; anchor = cl[a0]; G = gm * atr[a0]
            if G <= 0:
                continue
            longs, shorts = {}, {}; offl = offs = False; pnl = 0.0; ncyc = 0
            for i in range(a0 + 1, len(cl)):
                h, l = hi[i], lo[i]
                for k in range(1, K + 1):
                    lv, sv = anchor - k * G, anchor + k * G
                    if not offl and k not in longs and l <= lv: longs[k] = (lv, i)
                    if not offs and k not in shorts and h >= sv: shorts[k] = (sv, i)
                if stop == "beyond2G":
                    sl_, ss_ = anchor - (K + 2) * G, anchor + (K + 2) * G
                    if longs and l <= sl_:
                        for e, _ in longs.values(): pnl += (sl_ - e) * PV - RT
                        longs = {}; offl = True
                    if shorts and h >= ss_:
                        for e, _ in shorts.values(): pnl += (e - ss_) * PV - RT
                        shorts = {}; offs = True
                for k, (e, ei) in list(longs.items()):
                    if i > ei and h >= e + G: pnl += G * PV - RT; ncyc += 1; del longs[k]
                for k, (e, ei) in list(shorts.items()):
                    if i > ei and l <= e - G: pnl += G * PV - RT; ncyc += 1; del shorts[k]
            last = cl[-1]
            for e, _ in longs.values(): pnl += (last - e) * PV - RT
            for e, _ in shorts.values(): pnl += (e - last) * PV - RT
            a = agg.setdefault(split_of(s), [0.0, 0, 0, []])
            a[0] += pnl; a[1] += ncyc; a[2] += 1; a[3].append(pnl)
        for sp, (pnl, ncyc, ns, per) in agg.items():
            per = np.array(per)
            rows.append(dict(data=label, strat=f"GRID K{K} g{gm} {stop}", split=sp, n=ns, net=round(pnl),
                             net_per_sess=round(pnl / ns, 1),
                             t_sess=round(per.mean() / (per.std(ddof=1) / np.sqrt(ns)), 2) if ns > 1 else None))
    return rows

if __name__ == "__main__":
    tx = g.load_1m("TXFR1_5m.parquet")
    txc = tx.copy(); txc["Open"] = txc.High = txc.Low = txc.Close
    txc["High"] = txc.Close; txc["Low"] = txc.Close
    txc_recent = txc[txc.index >= "2025-07-01"]          # control only needs a recent window
    o2 = load_oos2("oos2")
    print("OOS2 1m rows:", len(o2), "sessions:", o2.sess.nunique(), o2.index.min(), o2.index.max())
    configs = list(itertools.product((1, 2), (0.25, 0.5, 1.0), ("none", "beyond2G")))
    rows = []
    for one, label in ((tx, "TX"), (txc_recent, "TX_closeonly"), (o2, "OOS2_closeonly")):
        rows += bb_eval(one, label)
        rows += grid_eval(one, label, configs)
        print("done", label, flush=True)
    out = pd.DataFrame(rows)
    out.to_csv("atr_norm_results.csv", index=False)
    pd.set_option("display.width", 220); pd.set_option("display.max_rows", 300)
    print(out.to_string(index=False))
