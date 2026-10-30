"""Controls for bt_bbmr: (a) reversed direction gross check, (b) 15m bars,
(c) slippage 0 to see raw signal edge. Same fixed rules otherwise."""
import sys, os
import numpy as np, pandas as pd
import bt_bbmr as m

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

b5 = m.indicators(m.load_5m("TXFR1_5m.parquet"))

def run_rev(b, **kw):
    t = m.run(b, **kw)
    return t

rows = []
cfgs = [("P2", dict(mode="P2")), ("P3 adx<20", dict(mode="P2", adx_max=20, slope_max=0.5, bwr_max=1.5))]
for name, kw in cfgs:
    for slip in (0.0, 1.0):
        t = m.run(b5, slip=slip, **kw)
        for sp in m.SPLITS:
            s = m.split(t, sp)
            rows.append(dict(cfg=name, bars="5m", slip=slip, split=sp, n=len(s),
                             gross_pts_per_trade=round(s.gross.mean() / m.PV, 2) if len(s) else None))
# reversed-direction control: flip the sign of every trade's gross at slip=0
# (a pure sign flip of the same entries/exits -- isolates whether the
# mean-reversion direction carries any raw edge vs its mirror)
for name, kw in cfgs:
    t = m.run(b5, slip=0.0, **kw)
    for sp in m.SPLITS:
        s = m.split(t, sp)
        rows.append(dict(cfg=name + " REVERSED", bars="5m", slip=0.0, split=sp, n=len(s),
                         gross_pts_per_trade=round(-s.gross.mean() / m.PV, 2) if len(s) else None))
# 15m bars robustness
raw5 = m.load_5m("TXFR1_5m.parquet")
raw5["b15"] = raw5.b5.dt.floor("15min")
b15 = raw5.groupby(["sess", "b15"], sort=True).agg(Open=("Open", "first"), High=("High", "max"),
        Low=("Low", "min"), Close=("Close", "last"), Volume=("Volume", "sum")).reset_index()
b15 = b15.rename(columns={"b15": "b5"}).sort_values("b5").reset_index(drop=True)
b15 = m.indicators(b15)
for name, kw in cfgs:
    t = m.run(b15, slip=1.0, max_hold=8, **{k: v for k, v in kw.items()})
    for sp in m.SPLITS:
        s = m.split(t, sp)
        st = m.stats(s)
        rows.append(dict(cfg=name, bars="15m", slip=1.0, split=sp, n=st.get("n"),
                         gross_pts_per_trade=round(s.gross.mean() / m.PV, 2) if len(s) else None,
                         net=st.get("net"), pf=st.get("pf")))
pd.set_option("display.width", 200)
print(pd.DataFrame(rows).to_string(index=False))
