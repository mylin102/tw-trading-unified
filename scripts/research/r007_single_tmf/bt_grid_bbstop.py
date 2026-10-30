"""Grid with Bollinger-band stops.
- 5m BB(20,2) from completed 5m bars; each 1m bar sees the band of the LAST
  COMPLETED 5m bar only (merge_asof on completion time, no look-ahead).
- Grid anchored on the current BB mid: long level k = mid - k*G, short level
  k = mid + k*G, only armed if the level is still inside the band.
  One open lot per (side, k) at a time. Conservative: opens before closes.
- TP: entry +/- G.
- STOP mode 'touch': 1m Low <= lower band (longs) / High >= upper band (shorts),
  fill at band (gap -> Open).  mode 'close5': completed 5m Close outside band,
  fill at next 1m Open.
- After a stop: rearm=False -> side off for the session; rearm=True -> side
  re-enabled once a completed 5m Close is back inside the band.
- Session end force-close. Cost 58.9 TWD/RT, 10 TWD/pt. K<=2 (margin-feasible).
"""
import sys, os, itertools
import numpy as np, pandas as pd
import bt_grid as g
import bt_bbmr as m

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")
PV, RT = g.PV, g.RT

one = g.load_1m("TXFR1_5m.parquet")
b5 = m.indicators(m.load_5m("TXFR1_5m.parquet"))
b5["done"] = b5.b5 + pd.Timedelta(minutes=5)
band = b5[["done", "sess", "mid", "up", "lo", "Close", "warm"]].rename(columns={"sess": "bsess", "Close": "c5"})
one = one.reset_index().rename(columns={one.index.name or "index": "ts"})
one["t_end"] = one.ts + pd.Timedelta(minutes=1)
one = pd.merge_asof(one.sort_values("t_end"), band.sort_values("done"),
                    left_on="t_end", right_on="done", direction="backward")
# band only valid if it comes from the same session and is warmed up
ok = (one.bsess == one.sess) & one.warm.fillna(False).astype(bool)
one.loc[~ok, ["mid", "up", "lo", "c5"]] = np.nan
# flag: this 1m bar is the first one after a new 5m completion
one["new5"] = one.done.ne(one.done.shift())

def session(x, G, K, mode, rearm):
    op, hi, lo_, cl = x.Open.to_numpy(), x.High.to_numpy(), x.Low.to_numpy(), x.Close.to_numpy()
    mid, up, lb, c5, new5 = x.mid.to_numpy(), x.up.to_numpy(), x.lo.to_numpy(), x.c5.to_numpy(), x.new5.to_numpy()
    longs, shorts = {}, {}  # k -> entry
    off_l = off_s = False; pend_l = pend_s = False
    tp = sl = fc = 0.0; ntp = nsl = nfc = 0
    for i in range(len(cl)):
        if np.isnan(mid[i]):
            continue
        o, h, l = op[i], hi[i], lo_[i]
        # pending close5 stops fill at this bar's open
        if pend_l and longs:
            for e in longs.values(): sl += (o - e) * PV - RT; nsl += 1
            longs = {}; off_l = True
        if pend_s and shorts:
            for e in shorts.values(): sl += (e - o) * PV - RT; nsl += 1
            shorts = {}; off_s = True
        pend_l = pend_s = False
        if rearm and new5[i] and not np.isnan(c5[i]) and lb[i] < c5[i] < up[i]:
            off_l = off_s = False
        for k in range(1, K + 1):
            lv = mid[i] - k * G
            if not off_l and k not in longs and lv > lb[i] and l <= lv:
                longs[k] = min(lv, o)
            sv = mid[i] + k * G
            if not off_s and k not in shorts and sv < up[i] and h >= sv:
                shorts[k] = max(sv, o)
        if mode == "touch":
            if longs and l <= lb[i]:
                px = min(lb[i], o)
                for e in longs.values(): sl += (px - e) * PV - RT; nsl += 1
                longs = {}; off_l = True
            if shorts and h >= up[i]:
                px = max(up[i], o)
                for e in shorts.values(): sl += (e - px) * PV - RT; nsl += 1
                shorts = {}; off_s = True
        else:  # close5: decided on a completed 5m close, filled next bar open
            if new5[i] and not np.isnan(c5[i]):
                if longs and c5[i] < lb[i]: pend_l = True
                if shorts and c5[i] > up[i]: pend_s = True
        for k, e in list(longs.items()):
            if h >= e + G: tp += G * PV - RT; ntp += 1; del longs[k]
        for k, e in list(shorts.items()):
            if l <= e - G: tp += G * PV - RT; ntp += 1; del shorts[k]
    last = cl[-1]
    for e in longs.values(): fc += (last - e) * PV - RT; nfc += 1
    for e in shorts.values(): fc += (e - last) * PV - RT; nfc += 1
    return np.array([tp, sl, fc, ntp, nsl, nfc])

groups = [(s, x) for s, x in one.groupby("sess", sort=True)]
def split_of(s):
    return "train" if s < "2025-01-01" else ("valid" if s < "2026-01-01" else "oos")
rows = []
for K, G, mode, rearm in itertools.product((1, 2), (10, 20, 30), ("touch", "close5"), (False, True)):
    agg = {}
    for s, x in groups:
        a = agg.setdefault(split_of(s), np.zeros(7))
        a[:6] += session(x, G, K, mode, rearm); a[6] += 1
    for sp in ("train", "valid", "oos"):
        tp, sl, fc, ntp, nsl, nfc, ns = agg[sp]
        rows.append(dict(K=K, G=G, stop=mode, rearm=rearm, split=sp, tp_n=int(ntp), tp=round(tp),
                         stop_n=int(nsl), stop_pnl=round(sl), forced_n=int(nfc), forced=round(fc),
                         net=round(tp + sl + fc)))
out = pd.DataFrame(rows)
out.to_csv("grid_bbstop_results.csv", index=False)
p = out.pivot_table(index=["K", "G", "stop", "rearm"], columns="split", values="net")[["train", "valid", "oos"]]
pd.set_option("display.width", 200)
print("configs:", len(p))
print(p.sort_values("train", ascending=False).to_string())
print("positive in BOTH train and valid:", int(((p.train > 0) & (p.valid > 0)).sum()))
