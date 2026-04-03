#!/usr/bin/env python3
"""比較不同 EMA 週期對 bull_align guard 的影響"""
import sys
import pathlib
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
vwap = df_5m["vwap"].values
score_arr = np.array(scores)
sqz_on = df_5m["sqz_on"].values
mom_state = df_5m["mom_state"].values
n = len(df_5m)

ENTRY_SCORE = 20
SL_PTS = 60
PV = 10

def run(label, bull_arr, bear_arr):
    pos = 0
    entry_p = 0
    pnl = 0
    trades = 0
    wins = 0
    blocked = 0
    for i in range(1, n):
        c, s = close[i], score_arr[i]
        if pos != 0:
            pts = (c - entry_p) * pos
            if pts <= -SL_PTS:
                pnl -= SL_PTS * PV
                trades += 1
                pos = 0
                continue
            if pts >= SL_PTS:
                pnl += pts * PV
                trades += 1
                wins += 1
                pos = 0
                continue
        if pos == 0:
            can_long = bull_arr[i] is not False  # allow if bull or neutral
            can_short = bear_arr[i] is not False  # allow if bear or neutral
            # bull_align guard
            if bull_arr[i]:
                can_short = False
            if bear_arr[i]:
                can_long = False
            
            sqz_buy = (not sqz_on[i]) and s >= ENTRY_SCORE and mom_state[i] >= 2 and c > vwap[i]
            sqz_sell = (not sqz_on[i]) and s <= -ENTRY_SCORE and mom_state[i] <= 1 and c < vwap[i]
            
            if sqz_buy and can_long:
                pos = 1
                entry_p = c
            elif sqz_sell and can_short:
                pos = -1
                entry_p = c
            elif (sqz_buy and not can_long) or (sqz_sell and not can_short):
                blocked += 1

    wr = wins / trades * 100 if trades else 0
    avg = pnl / trades if trades else 0
    # count crossovers
    crosses = sum(1 for i in range(1, len(bull_arr)) if bull_arr[i] != bull_arr[i-1])
    print(f"  [{label}] 交易:{trades} 勝率:{wr:.1f}% 淨損益:{pnl:+,.0f} 平均:{avg:+,.0f} 擋掉:{blocked} 交叉次數:{crosses}")

print(f"數據: {df_5m.index[0].date()} ~ {df_5m.index[-1].date()}\n")

# 不同 EMA 週期
configs = [
    ("EMA4/12 (20m/60m)", 4, 12),
    ("EMA8/24 (40m/2h)", 8, 24),
    ("EMA12/36 (1h/3h)", 12, 36),
    ("EMA20/60 (現行 100m/5h)", 20, 60),
    ("EMA40/120 (200m/10h)", 40, 120),
]

for label, fast, slow in configs:
    ema_f = _ema(df_5m["Close"], fast).values
    ema_s = _ema(df_5m["Close"], slow).values
    bull = ema_f > ema_s
    bear = ema_f < ema_s
    run(label, bull, bear)
