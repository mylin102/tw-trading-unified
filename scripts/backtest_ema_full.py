#!/usr/bin/env python3
"""完整回測：EMA12/36 vs EMA20/60，含 TP1 + trailing + VWAP 出場"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment, _ema

DATA = pathlib.Path.home() / "Documents/mylin102/tw-futures-realtime/data/taifex_raw/TMF_5m_taifex.csv"
df_raw = pd.read_csv(DATA, parse_dates=["ts"], index_col="ts")
df_5m = calculate_futures_squeeze(df_raw)
df_15m = calculate_futures_squeeze(
    df_raw.resample("15min", label="right", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Open"])
)
df_15m_a = df_15m[["Close", "ema_filter", "momentum", "mom_state"]].reindex(df_5m.index, method="ffill")
df_1h = calculate_futures_squeeze(
    df_raw.resample("1h", label="right", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Open"])
)
df_1h_a = df_1h[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")

scores = []
for i in range(len(df_5m)):
    d = {"5m": pd.DataFrame([{"momentum": df_5m.iloc[i]["momentum"], "mom_state": df_5m.iloc[i]["mom_state"]}])}
    if not pd.isna(df_15m_a.iloc[i]["momentum"]):
        d["15m"] = pd.DataFrame([{"momentum": df_15m_a.iloc[i]["momentum"], "mom_state": df_15m_a.iloc[i]["mom_state"]}])
    if not pd.isna(df_1h_a.iloc[i]["momentum"]):
        d["1h"] = pd.DataFrame([{"momentum": df_1h_a.iloc[i]["momentum"], "mom_state": df_1h_a.iloc[i]["mom_state"]}])
    scores.append(calculate_mtf_alignment(d)["score"])

close = df_5m["Close"].values
high = df_5m["High"].values
low = df_5m["Low"].values
vwap = df_5m["vwap"].values
score_arr = np.array(scores)
sqz_on = df_5m["sqz_on"].values
mom_st = df_5m["mom_state"].values
n = len(df_5m)

ENTRY_SCORE = 20
SL_PTS = 60
TP1_PTS = 50
PV = 10
LOTS = 2

def run(label, bull, bear):
    pos = 0; direction = 0; entry_p = 0; lots = 0
    has_tp1 = False; peak = 0
    pnl = 0; trades = 0; wins = 0
    reasons = {}

    for i in range(1, n):
        c, s, h, l, v = close[i], score_arr[i], high[i], low[i], vwap[i]

        if pos != 0:
            # TP1
            if not has_tp1 and lots == LOTS:
                tp_hit = (h >= entry_p + TP1_PTS) if direction == 1 else (l <= entry_p - TP1_PTS)
                if tp_hit:
                    tp_price = entry_p + TP1_PTS * direction
                    pnl += TP1_PTS * PV
                    lots = LOTS - 1
                    has_tp1 = True
                    trades += 1; wins += 1
                    reasons["TP1"] = reasons.get("TP1", 0) + 1
                    peak = tp_price

            # Trailing (after TP1, track peak, exit if retrace 30pts)
            if has_tp1 and lots > 0:
                if direction == 1:
                    if h > peak: peak = h
                    if c <= peak - 30:
                        pnl += (c - entry_p) * PV * lots
                        if c > entry_p: wins += 1
                        trades += 1; lots = 0; pos = 0
                        reasons["TRAILING"] = reasons.get("TRAILING", 0) + 1
                        continue
                else:
                    if l < peak: peak = l
                    if c >= peak + 30:
                        pnl += (entry_p - c) * PV * lots
                        if c < entry_p: wins += 1
                        trades += 1; lots = 0; pos = 0
                        reasons["TRAILING"] = reasons.get("TRAILING", 0) + 1
                        continue

            # Stop loss
            unrealized = (c - entry_p) * direction
            if unrealized <= -SL_PTS:
                pnl += -SL_PTS * PV * lots
                trades += 1; lots = 0; pos = 0
                reasons["STOP_LOSS"] = reasons.get("STOP_LOSS", 0) + 1
                continue

            # VWAP exit (after TP1, price crosses VWAP against direction)
            if has_tp1 and lots > 0:
                if (direction == 1 and c < v) or (direction == -1 and c > v):
                    gain = (c - entry_p) * direction * PV * lots
                    pnl += gain
                    if gain > 0: wins += 1
                    trades += 1; lots = 0; pos = 0
                    reasons["VWAP"] = reasons.get("VWAP", 0) + 1
                    continue

        if pos == 0:
            # regime filter (mid) + align guard
            can_long = df_15m_a.iloc[i]["Close"] > df_15m_a.iloc[i]["ema_filter"] * 0.998 if not pd.isna(df_15m_a.iloc[i]["ema_filter"]) else True
            can_short = df_15m_a.iloc[i]["Close"] < df_15m_a.iloc[i]["ema_filter"] * 1.002 if not pd.isna(df_15m_a.iloc[i]["ema_filter"]) else True
            if bull[i]: can_short = False
            if bear[i]: can_long = False

            sqz_buy = (not sqz_on[i]) and s >= ENTRY_SCORE and mom_st[i] >= 2 and c > v
            sqz_sell = (not sqz_on[i]) and s <= -ENTRY_SCORE and mom_st[i] <= 1 and c < v

            if sqz_buy and can_long:
                pos = 1; direction = 1; entry_p = c; lots = LOTS; has_tp1 = False; peak = c
            elif sqz_sell and can_short:
                pos = -1; direction = -1; entry_p = c; lots = LOTS; has_tp1 = False; peak = c

    wr = wins / trades * 100 if trades else 0
    avg = pnl / trades if trades else 0
    max_dd = 0; running = 0; peak_pnl = 0
    # rough drawdown from trade-level
    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"  交易: {trades}  勝率: {wr:.1f}%  淨損益: {pnl:+,.0f} TWD  平均: {avg:+,.0f}")
    for r, cnt in sorted(reasons.items()):
        print(f"    {r}: {cnt}")

print(f"數據: {df_5m.index[0].date()} ~ {df_5m.index[-1].date()} ({n} bars)\n")

for label, fast, slow in [("EMA12/36 (1h/3h)", 12, 36), ("EMA20/60 (現行)", 20, 60)]:
    ef = _ema(df_5m["Close"], fast).values
    es = _ema(df_5m["Close"], slow).values
    run(label, ef > es, ef < es)
