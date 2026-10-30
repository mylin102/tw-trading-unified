"""TMF BB mean-reversion research backtest (spec Gate 1, P1-P4).

Rules are fixed BEFORE looking at results (spec 4.x / 8.2):
- Data: TXFR1 1-minute OHLCV (TX near-month continuous; same index/expiry as
  TMF). Only raw Open/High/Low/Close are used -- the parquet's precomputed
  indicator columns are ignored (unknown construction / look-ahead risk).
- 5m bars built per session (day 08:45-13:45, night 15:00-05:00).
- Indicators on completed 5m closes: BB(20,2), Wilder ATR(14), Wilder ADX(14),
  midline slope = (mid - mid[-5]) / ATR, bandwidth ratio = bbw / bbw[-5].
- Signal on completed bar i, fill at bar i+1 Open +/- slippage, same session only.
- Exit: completed close crosses midline -> next open; ATR stop intrabar
  (gap-through -> fill at Open); max-hold bars -> next open; session end ->
  last close. One position at a time, max 1 lot, no adds.
- Cost: 58.9 TWD round trip (fees+tax, ledger-verified) + slippage pts each
  side, TMF point value 10 TWD.
- Split: train 2023-04..2024-12 / valid 2025 / OOS 2026-01..06.
"""
import sys, os, itertools, json
import numpy as np
import pandas as pd

if __name__ == "__main__" and sys.platform == "darwin":
    os.system(f"taskpolicy -b -p {os.getpid()}")

PV = 10.0
RT_COST = 58.9

def load_5m(path):
    raw = pd.read_parquet(path, columns=["Open", "High", "Low", "Close", "Volume"])
    raw = raw[~raw.index.duplicated()].sort_index()
    # 12 source minutes carry NaN O/H/L with a valid Close -> drop them
    # rather than invent a range (counted and reported).
    bad = raw[["Open", "High", "Low"]].isna().any(axis=1)
    print("dropped 1m rows with NaN O/H/L:", int(bad.sum()))
    raw = raw[~bad]
    t = raw.index
    hm = t.hour * 60 + t.minute
    day = (hm >= 8 * 60 + 45) & (hm < 13 * 60 + 45)
    night_eve = hm >= 15 * 60
    night_morn = hm < 5 * 60
    keep = day | night_eve | night_morn
    raw = raw[keep]
    t = raw.index; hm = t.hour * 60 + t.minute
    d = t.normalize()
    sess_date = np.where(hm < 5 * 60, d - pd.Timedelta(days=1), d)
    sess_kind = np.where((hm >= 8 * 60 + 45) & (hm < 13 * 60 + 45), "D", "N")
    raw = raw.assign(sess=pd.Series(sess_date, index=raw.index).dt.strftime("%Y-%m-%d") + sess_kind,
                     b5=raw.index.floor("5min"))
    g = raw.groupby(["sess", "b5"], sort=True)
    bars = g.agg(Open=("Open", "first"), High=("High", "max"), Low=("Low", "min"),
                 Close=("Close", "last"), Volume=("Volume", "sum")).reset_index()
    bars = bars.sort_values("b5").reset_index(drop=True)
    return bars

def indicators(b, n=20, k=2.0, w=14):
    c, h, l = b.Close, b.High, b.Low
    mid = c.rolling(n).mean(); sd = c.rolling(n).std(ddof=0)
    b["mid"], b["up"], b["lo"] = mid, mid + k * sd, mid - k * sd
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / w, adjust=False).mean()
    upm = h.diff(); dnm = -l.diff()
    pdm = np.where((upm > dnm) & (upm > 0), upm, 0.0)
    ndm = np.where((dnm > upm) & (dnm > 0), dnm, 0.0)
    pdi = 100 * pd.Series(pdm).ewm(alpha=1 / w, adjust=False).mean() / atr
    ndi = 100 * pd.Series(ndm).ewm(alpha=1 / w, adjust=False).mean() / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    b["adx"] = dx.ewm(alpha=1 / w, adjust=False).mean()
    b["atr"] = atr
    b["slope"] = (mid - mid.shift(5)) / atr
    bbw = (b.up - b.lo) / mid
    b["bwr"] = bbw / bbw.shift(5)
    b["warm"] = np.arange(len(b)) >= 60
    return b

def run(b, mode, adx_max=None, slope_max=None, bwr_max=None, stop_k=2.0, max_hold=24, slip=1.0):
    O, H, L, C = (b[x].to_numpy() for x in ("Open", "High", "Low", "Close"))
    mid, up, lo, atr = (b[x].to_numpy() for x in ("mid", "up", "lo", "atr"))
    adx, slope, bwr, warm = b.adx.to_numpy(), b.slope.to_numpy(), b.bwr.to_numpy(), b.warm.to_numpy()
    sess = b.sess.to_numpy()
    n = len(b); trades = []; i = 1
    pos = 0; entry = stop = 0.0; ei = 0; esess = ""
    while i < n - 1:
        if pos != 0:
            # i is a completed bar while in position (entered at ei's open)
            exit_px = None; reason = None
            if pos > 0 and L[i] <= stop:
                exit_px = min(stop, O[i]) - slip; reason = "stop"
            elif pos < 0 and H[i] >= stop:
                exit_px = max(stop, O[i]) + slip; reason = "stop"
            elif sess[i + 1] != sess[i]:
                exit_px = C[i] - slip * pos; reason = "session_end"
            elif (pos > 0 and C[i] >= mid[i]) or (pos < 0 and C[i] <= mid[i]):
                exit_px = O[i + 1] - slip * pos; reason = "mid"
            elif i - ei + 1 >= max_hold:
                exit_px = O[i + 1] - slip * pos; reason = "max_hold"
            if exit_px is not None:
                gross = (exit_px - entry) * pos * PV
                trades.append(dict(entry_ts=b.b5.iat[ei], sig_i=ei - 1, sess=esess, side=pos, gross=gross,
                                   net=gross - RT_COST, reason=reason, hold=i - ei + 1))
                pos = 0
                i += 1 if reason in ("stop", "session_end") else 1
                continue
            i += 1; continue
        if not warm[i] or np.isnan(atr[i]) or sess[i + 1] != sess[i]:
            i += 1; continue
        sig = 0
        if mode == "P1":
            if L[i] <= lo[i]: sig = 1
            elif H[i] >= up[i]: sig = -1
        else:  # P2 base: prior close outside band, this close back inside
            if sess[i - 1] == sess[i]:
                if C[i - 1] < lo[i - 1] and C[i] >= lo[i]: sig = 1
                elif C[i - 1] > up[i - 1] and C[i] <= up[i]: sig = -1
        if sig and adx_max is not None and not (adx[i] < adx_max): sig = 0
        if sig and slope_max is not None and not (abs(slope[i]) < slope_max): sig = 0
        if sig and bwr_max is not None and not (bwr[i] < bwr_max): sig = 0
        if sig:
            pos = sig; ei = i + 1; esess = sess[ei]
            entry = O[ei] + slip * sig
            stop = entry - sig * stop_k * atr[i]
            i = ei  # evaluate exits starting on the entry bar itself
            continue
        i += 1
    return pd.DataFrame(trades)

def stats(t):
    if t is None or len(t) == 0:
        return dict(n=0)
    net = t.net.to_numpy(); eq = np.cumsum(net)
    dd = (eq - np.maximum.accumulate(np.concatenate([[0], eq]))[1:]).min()
    wins, losses = net[net > 0].sum(), -net[net <= 0].sum()
    return dict(n=len(t), win=round((net > 0).mean() * 100, 1), gross=round(t.gross.sum()),
                net=round(net.sum()), pf=round(wins / losses, 2) if losses else None,
                exp=round(net.mean(), 1), maxdd=round(dd), hold=round(t.hold.mean(), 1),
                cost_pct_gross=round(len(t) * RT_COST / t.gross.sum() * 100, 0) if t.gross.sum() > 0 else None)

SPLITS = {"train": ("2023-04-01", "2025-01-01"), "valid": ("2025-01-01", "2026-01-01"),
          "oos": ("2026-01-01", "2026-07-01")}

def split(t, name):
    a, z = SPLITS[name]
    return t[(t.entry_ts >= a) & (t.entry_ts < z)]

if __name__ == "__main__":
    b = indicators(load_5m(sys.argv[1]))
    print("5m bars:", len(b), b.b5.min(), b.b5.max())
    configs = [("P1 touch", dict(mode="P1")),
               ("P2 close-back", dict(mode="P2"))]
    for adx_max in (20, 25, 30):
        configs.append((f"P3 adx<{adx_max}+slope<0.5+bwr<1.5",
                        dict(mode="P2", adx_max=adx_max, slope_max=0.5, bwr_max=1.5)))
    for stop_k, mh in itertools.product((1.5, 2.0, 3.0), (12, 24, 48)):
        configs.append((f"P4 adx<25 stop{stop_k}ATR hold{mh}",
                        dict(mode="P2", adx_max=25, slope_max=0.5, bwr_max=1.5, stop_k=stop_k, max_hold=mh)))
    rows = []
    for name, kw in configs:
        t = run(b, **kw)
        for sp in SPLITS:
            rows.append(dict(cfg=name, split=sp, **stats(split(t, sp))))
    out = pd.DataFrame(rows)
    pd.set_option("display.width", 250); pd.set_option("display.max_rows", 200)
    print(out.to_string(index=False))
    out.to_csv("results_p1_p4.csv", index=False)
    print(f"\nTOTAL CONFIGS TESTED: {len(configs)} (report all, no cherry-pick)")
