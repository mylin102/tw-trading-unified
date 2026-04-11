#!/usr/bin/env python3
"""
回測 IV 過濾對 V2 買方策略的影響：
  A) V2 現行（無 IV 過濾）
  B) V2 + IV 上限（IV > 35% 不進場）
  C) V2 + IV 上限 30%
  D) V2 + IV 噴發加速進場（IV 5min 漲 > 2%）
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "strategies" / "options"))

import pandas as pd
import numpy as np
from strategies.options.options_engine.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment

DATA = pathlib.Path(__file__).resolve().parent.parent / "data" / "tmf_replay_5min_q1_2026.csv"
df_raw = pd.read_csv(DATA, parse_dates=["datetime"], index_col="datetime")

df_5m = calculate_futures_squeeze(df_raw)
df_15m = calculate_futures_squeeze(
    df_raw.resample("15min", label="right", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Open"])
)
df_1h = calculate_futures_squeeze(
    df_raw.resample("1h", label="right", closed="left").agg(
        {"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}
    ).dropna(subset=["Open"])
)

df_15m_a = df_15m[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")
df_1h_a = df_1h[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")

scores = []
for i in range(len(df_5m)):
    d = {"5m": pd.DataFrame([{"momentum": df_5m.iloc[i]["momentum"], "mom_state": df_5m.iloc[i]["mom_state"]}])}
    if not pd.isna(df_15m_a.iloc[i]["momentum"]):
        d["15m"] = pd.DataFrame([{"momentum": df_15m_a.iloc[i]["momentum"], "mom_state": df_15m_a.iloc[i]["mom_state"]}])
    if not pd.isna(df_1h_a.iloc[i]["momentum"]):
        d["1h"] = pd.DataFrame([{"momentum": df_1h_a.iloc[i]["momentum"], "mom_state": df_1h_a.iloc[i]["mom_state"]}])
    scores.append(calculate_mtf_alignment(d, weights={"5m": 0.2, "15m": 0.4, "1h": 0.4})["score"])

close = df_5m["Close"].values
vwap = df_5m["vwap"].values
score_arr = np.array(scores)
fired = df_5m["fired"].values
bull = df_5m["bullish_align"].values
bear = df_5m["bearish_align"].values
n = len(df_5m)

# Simulate IV using realized volatility (20-bar rolling std * sqrt(252*78))
returns = pd.Series(close).pct_change().fillna(0).values
iv_arr = np.zeros(n)
for i in range(20, n):
    iv_arr[i] = np.std(returns[i-20:i]) * np.sqrt(252 * 78)  # annualized
# Fill first 20 bars
iv_arr[:20] = iv_arr[20] if iv_arr[20] > 0 else 0.25

# IV change (5 bars = 25 min)
iv_change = np.zeros(n)
for i in range(5, n):
    iv_change[i] = iv_arr[i] - iv_arr[i-5]

PV = 50
DELTA = 0.5
ENTRY_P = 100
THETA = 0.3
ENTRY_SCORE = 60
SL_PCT = 0.20
TP_PCT = 0.8
COOLDOWN = 3
TRAIL = 0.15

print(f"數據: {df_5m.index[0].date()} ~ {df_5m.index[-1].date()} ({n} bars)")
print(f"IV 範圍: {iv_arr[20:].min()*100:.1f}% ~ {iv_arr[20:].max()*100:.1f}%, 均值: {iv_arr[20:].mean()*100:.1f}%")

def run(label, iv_max, iv_spike_entry):
    pos = 0
    entry_p = 0
    entry_mtx = 0
    has_tp1 = False
    side = None
    peak = 0
    cd = 0
    pnl = 0
    trades = 0
    wins = 0
    blocked_iv = 0
    spike_entries = 0

    for i in range(20, n):
        c, s, iv = close[i], score_arr[i], iv_arr[i]
        if pos > 0:
            diff = (c - entry_mtx) * (1 if side == "C" else -1)
            cur = entry_p + diff * DELTA - THETA
            cur = max(cur, 0.1)
            if cur > peak:
                peak = cur
            if not has_tp1 and pos == 2 and (cur - entry_p) / entry_p >= TP_PCT:
                pnl += (cur - entry_p) * PV
                trades += 1
                wins += 1
                pos = 1
                has_tp1 = True
            if TRAIL > 0 and has_tp1 and cur <= peak * (1 - TRAIL):
                pnl += (cur - entry_p) * PV * pos
                trades += 1
                if cur > entry_p:
                    wins += 1
                pos = 0
                cd = COOLDOWN
                continue
            threshold = entry_p if has_tp1 else entry_p * (1 - SL_PCT)
            if cur <= threshold:
                pnl += (cur - entry_p) * PV * pos
                trades += 1
                pos = 0
                cd = COOLDOWN
                continue

        if pos == 0:
            if cd > 0:
                cd -= 1
                continue

            # IV spike entry: bypass score requirement
            if iv_spike_entry and iv_change[i] > 0.02:
                if bull[i] and c > vwap[i]:
                    side = "C"
                    spike_entries += 1
                    pos = 2
                    entry_p = ENTRY_P
                    entry_mtx = c
                    has_tp1 = False
                    peak = ENTRY_P
                    continue
                elif bear[i] and c < vwap[i]:
                    side = "P"
                    spike_entries += 1
                    pos = 2
                    entry_p = ENTRY_P
                    entry_mtx = c
                    has_tp1 = False
                    peak = ENTRY_P
                    continue

            # Normal V2 entry
            can_enter = (fired[i] or abs(s) >= 90)
            if not can_enter:
                continue
            if s >= ENTRY_SCORE and c > vwap[i] and bull[i]:
                side = "C"
            elif s <= -ENTRY_SCORE and c < vwap[i] and bear[i]:
                side = "P"
            else:
                continue

            # IV filter
            if iv_max > 0 and iv > iv_max:
                blocked_iv += 1
                continue

            pos = 2
            entry_p = ENTRY_P
            entry_mtx = c
            has_tp1 = False
            peak = ENTRY_P

    wr = wins / trades * 100 if trades else 0
    avg = pnl / trades if trades else 0
    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"  交易: {trades}  勝率: {wr:.1f}%  淨損益: {pnl:+,.0f} TWD  平均: {avg:+,.0f}")
    if blocked_iv:
        print(f"  IV 擋掉: {blocked_iv} 筆")
    if spike_entries:
        print(f"  IV 噴發進場: {spike_entries} 筆")

run("A) V2 現行（無 IV 過濾）", iv_max=0, iv_spike_entry=False)
run("B) V2 + IV 上限 35%", iv_max=0.35, iv_spike_entry=False)
run("C) V2 + IV 上限 30%", iv_max=0.30, iv_spike_entry=False)
run("D) V2 + IV 噴發加速（無上限）", iv_max=0, iv_spike_entry=True)
run("E) V2 + IV 上限 35% + 噴發加速", iv_max=0.35, iv_spike_entry=True)
