"""Market-behaviour scan (pre-registered conditions; all reported).
Unit = one trade per session per condition (independent-ish samples).
Prices from 1m closes only (so OOS2 close-only TMF data is usable).
Entry = first 1m close AT/AFTER the decision time (no look-ahead: decision
uses only data before it). Exit = 1m close at the stated exit time.
PnL in points, net of 7.9 pt (fees+tax 5.9 + 1 slip/side).
Normalised by sATR = mean |open-to-close| of the PRIOR 20 sessions of the
same kind (D/N) -- known before the session.
Sessions: D 08:45-13:45, N 15:00-05:00.  Requires >= 80% minutes present.

C1a  intraday momentum: dir = sign(ret open->+30m); hold +30m -> close
C1b  last-30m: dir = sign(ret open->close-30m); hold close-30m -> close
C2   gap fill: gap = open - prev session close; dir = -sign(gap) if |gap|>0.5 sATR;
     hold open(+1m) -> +60m   [and C2c: continuation = opposite sign, reported]
C3   ORB30: first 1m close outside the first-30m high/low (closes); dir = side; hold -> close
C4   drift: always LONG open->close, by session kind
C5   prev-session reversal: dir = -sign(prev session open->close); hold open->close
C6   day-of-week LONG open->close (D only), per weekday
C7   settlement day (3rd Wed) D session LONG open->close
"""
import sys, os
import numpy as np, pandas as pd
import bt_grid as g
import bt_atrnorm as an

if sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")
COST = 7.9

def sessions_from(one, label):
    out = []
    for s, x in one.groupby("sess", sort=True):
        c = x.Close.dropna()
        if len(c) < 30:
            continue
        kind = s[-1]
        span = 300 if kind == "D" else 840
        if len(c) < 0.8 * span:
            continue
        t0 = c.index[0]
        def at(minutes):  # first close at/after t0+minutes
            k = c.index.searchsorted(t0 + pd.Timedelta(minutes=minutes))
            return c.iloc[k] if k < len(c) else np.nan
        tend = c.index[-1]
        k30e = c.index.searchsorted(tend - pd.Timedelta(minutes=30))
        first30 = c[c.index < t0 + pd.Timedelta(minutes=30)]
        rest = c[c.index >= t0 + pd.Timedelta(minutes=30)]
        hi, lo = first30.max(), first30.min()
        brk = rest[(rest > hi) | (rest < lo)]
        orb_dir, orb_px = 0, np.nan
        if len(brk):
            orb_px = brk.iloc[0]; orb_dir = 1 if orb_px > hi else -1
        out.append(dict(sess=s, kind=kind, src=label, date=s[:10], open=c.iloc[0], close=c.iloc[-1],
                        p30=at(30), p60=at(60), p1=at(1), pc30=c.iloc[k30e] if k30e < len(c) else np.nan,
                        orb_dir=orb_dir, orb_px=orb_px))
    return pd.DataFrame(out)

def build():
    tx = g.load_1m("TXFR1_5m.parquet")
    o2 = an.load_oos2("oos2")
    S = pd.concat([sessions_from(tx, "TX"), sessions_from(o2, "TMF_C")], ignore_index=True)
    S = S.sort_values(["sess"]).reset_index(drop=True)
    S["oc"] = S.close - S.open
    S["prev_close"] = S.close.shift(1)
    S["prev_oc"] = S.oc.shift(1)
    S.loc[S.src != S.src.shift(1), ["prev_close", "prev_oc"]] = np.nan
    # gap only valid if previous session is the immediately preceding one (no data hole > 1 day)
    gapdays = (pd.to_datetime(S.date) - pd.to_datetime(S.date.shift(1))).dt.days
    S.loc[gapdays > 3, ["prev_close", "prev_oc"]] = np.nan
    S["satr"] = S.groupby("kind").oc.transform(lambda x: x.abs().shift(1).rolling(20, min_periods=10).mean())
    S["split"] = S.sess.map(an.split_of)
    d = pd.to_datetime(S.date)
    S["dow"] = d.dt.dayofweek
    S["settle"] = (S.dow == 2) & (d.dt.day >= 15) & (d.dt.day <= 21) & (S.kind == "D")
    return S

def trades(S):
    T = []
    def add(name, mask, direction, entry, exit_):
        x = S[mask].copy()
        dirv = direction[mask] if isinstance(direction, pd.Series) else direction
        x["pts"] = dirv * (exit_[mask] - entry[mask])
        x["cond"] = name
        x = x[np.isfinite(x.pts) & (x.pts.notna()) & x.satr.gt(0)]
        x = x[(dirv if np.isscalar(dirv) else dirv.loc[x.index]) != 0] if not np.isscalar(dirv) else x
        T.append(x[["sess", "kind", "split", "src", "cond", "pts", "satr"]])
    s30 = np.sign(S.p30 - S.open)
    for k in ("D", "N"):
        K = S.kind == k
        add(f"C1a mom30->close {k}", K, s30, S.p30, S.close)
        add(f"C1b mom->last30 {k}", K, np.sign(S.pc30 - S.open), S.pc30, S.close)
        big = (S.open - S.prev_close).abs() > 0.5 * S.satr
        gs = np.sign(S.open - S.prev_close)
        add(f"C2 gapfill 60m {k}", K & big, -gs, S.p1, S.p60)
        add(f"C2c gapcont 60m {k}", K & big, gs, S.p1, S.p60)
        add(f"C3 ORB30->close {k}", K & (S.orb_dir != 0), S.orb_dir.astype(float), S.orb_px, S.close)
        add(f"C4 long drift {k}", K, pd.Series(1.0, index=S.index), S.open, S.close)
        add(f"C5 prev reversal {k}", K & S.prev_oc.notna(), -np.sign(S.prev_oc), S.open, S.close)
    for w, nm in enumerate(["Mon", "Tue", "Wed", "Thu", "Fri"]):
        add(f"C6 long {nm} D", (S.kind == "D") & (S.dow == w), pd.Series(1.0, index=S.index), S.open, S.close)
    add("C7 settle-day long D", S.settle, pd.Series(1.0, index=S.index), S.open, S.close)
    return pd.concat(T, ignore_index=True)

if __name__ == "__main__":
    S = build()
    print("sessions:", S.groupby(["split", "kind"]).size().to_dict())
    T = trades(S)
    T["net"] = T.pts - COST
    T["net_s"] = T.net / T.satr
    T["gross_s"] = T.pts / T.satr
    rows = []
    for (c, sp), x in T.groupby(["cond", "split"]):
        n = len(x); se = x.net_s.std(ddof=1) / np.sqrt(n) if n > 1 else np.nan
        rows.append(dict(cond=c, split=sp, n=n, gross_pts=round(x.pts.mean(), 1),
                         net_pts=round(x.net.mean(), 1), net_satr=round(x.net_s.mean(), 3),
                         t=round(x.net_s.mean() / se, 2) if se else np.nan,
                         win=round((x.net > 0).mean() * 100, 0)))
    R = pd.DataFrame(rows)
    R.to_csv("behaviour_scan.csv", index=False); T.to_csv("behaviour_trades.csv", index=False)
    p = R.pivot_table(index="cond", columns="split", values=["net_pts", "t", "n"], aggfunc="first")
    cols = [(v, s) for v in ("net_pts", "t", "n") for s in ("train", "valid", "oos1", "oos2") if (v, s) in p.columns]
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
    print("conditions tested:", R.cond.nunique(), "(all reported; Bonferroni 5% two-sided for 27 tests ~ |t|>3.1)")
    print(p[cols].to_string())
    tt = R.pivot_table(index="cond", columns="split", values="t", aggfunc="first")
    np_ = R.pivot_table(index="cond", columns="split", values="net_pts", aggfunc="first")
    ok = (tt.train > 2) & (tt.valid > 2) & (np_.oos1 > 0) & (np_.oos2.fillna(1) > 0)
    print("\nPASS (train t>2 & valid t>2 & oos1>0 & oos2>0):", list(tt.index[ok]))
