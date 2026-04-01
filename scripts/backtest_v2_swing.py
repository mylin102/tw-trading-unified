#!/usr/bin/env python3
"""
V2 Swing 回測：較長天期 + trailing stop + cooldown + 1h 加權
vs V3 Night (現行)
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "strategies" / "options"))

import pandas as pd
import numpy as np
from strategies.options.options_engine.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from strategies.options.options_engine.engine.backtest_engine import should_exit_position, stop_threshold

DATA = pathlib.Path.home() / "Documents/mylin102/tw-option-squeeze-trading/exports/tmf_replay_5min_q1_2026.csv"
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

POINT_VALUE = 50
DELTA = 0.5
ENTRY_PREMIUM = 100
THETA_PER_BAR = 0.3  # 每根5m bar的theta衰減(點)

def calc_scores(w):
    """用指定權重算 score"""
    df_15m_a = df_15m[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")
    df_1h_a = df_1h[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")
    out = []
    for i in range(len(df_5m)):
        d = {"5m": pd.DataFrame([{"momentum": df_5m.iloc[i]["momentum"], "mom_state": df_5m.iloc[i]["mom_state"]}])}
        if not pd.isna(df_15m_a.iloc[i]["momentum"]):
            d["15m"] = pd.DataFrame([{"momentum": df_15m_a.iloc[i]["momentum"], "mom_state": df_15m_a.iloc[i]["mom_state"]}])
        if not pd.isna(df_1h_a.iloc[i]["momentum"]):
            d["1h"] = pd.DataFrame([{"momentum": df_1h_a.iloc[i]["momentum"], "mom_state": df_1h_a.iloc[i]["mom_state"]}])
        out.append(calculate_mtf_alignment(d, weights=w)["score"])
    return np.array(out)

close_arr = df_5m["Close"].values
vwap_arr = df_5m["vwap"].values
bull_arr = df_5m["bullish_align"].values
n = len(df_5m)

def run(label, scores, entry_score, sl_pct, tp1_pct, score_floor,
        cooldown, trailing_pct, theta_per_bar):
    pos = 0
    entry_p = 0.0
    entry_mtx = 0.0
    has_tp1 = False
    side = None
    peak_p = 0.0
    cd = 0
    total_pnl = 0.0
    wins = 0
    trades = 0
    reasons = {}

    for i in range(1, n):
        c = close_arr[i]
        s = scores[i]

        if pos > 0:
            diff = (c - entry_mtx) * (1 if side == "C" else -1)
            cur_p = entry_p + diff * DELTA - theta_per_bar
            entry_p -= theta_per_bar  # theta eats into entry baseline too? No, theta eats current value
            # Actually: cur_p = initial_entry + delta*move - theta*bars_held
            # Simpler: just track cur_p with theta decay
            cur_p = max(cur_p, 0.1)

            if cur_p > peak_p:
                peak_p = cur_p

            # TP1
            if not has_tp1 and pos == 2 and (cur_p - entry_p) / entry_p >= tp1_pct:
                pnl = (cur_p - entry_p) * POINT_VALUE
                total_pnl += pnl
                if pnl > 0: wins += 1
                trades += 1
                reasons["TP1"] = reasons.get("TP1", 0) + 1
                pos = 1
                has_tp1 = True

            # Trailing stop (after TP1)
            if trailing_pct > 0 and has_tp1 and peak_p > 0:
                if cur_p <= peak_p * (1 - trailing_pct):
                    pnl = (cur_p - entry_p) * POINT_VALUE * pos
                    total_pnl += pnl
                    if pnl > 0: wins += 1
                    trades += 1
                    reasons["TRAILING"] = reasons.get("TRAILING", 0) + 1
                    pos = 0
                    cd = cooldown
                    continue

            # Normal exit
            if should_exit_position(cur_p, entry_p, sl_pct, s, has_tp1, score_floor=score_floor):
                pnl = (cur_p - entry_p) * POINT_VALUE * pos
                total_pnl += pnl
                if pnl > 0: wins += 1
                trades += 1
                r = "STOP_LOSS" if cur_p <= stop_threshold(entry_p, sl_pct, has_tp1) else "SCORE_FLOOR"
                reasons[r] = reasons.get(r, 0) + 1
                pos = 0
                cd = cooldown
                continue

        if pos == 0:
            if cd > 0:
                cd -= 1
                continue
            if s >= entry_score and c > vwap_arr[i]:
                side = "C"
            elif s <= -entry_score and c < vwap_arr[i]:
                side = "P"
            else:
                continue
            pos = 2
            entry_mtx = c
            entry_p = ENTRY_PREMIUM
            has_tp1 = False
            peak_p = ENTRY_PREMIUM

    wr = (wins / trades * 100) if trades > 0 else 0
    avg = (total_pnl / trades) if trades > 0 else 0
    print(f"\n{'='*60}")
    print(f"[{label}]")
    print(f"  交易: {trades}  勝率: {wr:.1f}%  淨損益: {total_pnl:+,.0f} TWD  平均: {avg:+,.0f}")
    for r, cnt in sorted(reasons.items()):
        print(f"    {r}: {cnt} 筆")
    return total_pnl

print(f"數據: {df_5m.index[0].date()} ~ {df_5m.index[-1].date()} ({n} bars)")

# V3 Night (現行) weights
scores_v3 = calc_scores({"5m": 0.4, "15m": 0.4, "1h": 0.2})
# V2 Swing weights (1h 加重)
scores_v2 = calc_scores({"5m": 0.2, "15m": 0.4, "1h": 0.4})

print("\n" + "="*60)
print("V3 Night (現行參數)")
print("="*60)
run("V3: entry=80 sl=10% tp=100% sf=20 cd=0 trail=0",
    scores_v3, 80, 0.10, 1.0, 20, 0, 0, 0)

print("\n" + "="*60)
print("V2 Swing (新參數)")
print("="*60)
run("V2: entry=80 sl=30% tp=80% sf=20 cd=6 trail=15%",
    scores_v2, 80, 0.30, 0.8, 20, 6, 0.15, THETA_PER_BAR)

run("V2: entry=90 sl=30% tp=80% sf=20 cd=6 trail=15%",
    scores_v2, 90, 0.30, 0.8, 20, 6, 0.15, THETA_PER_BAR)

run("V2: entry=80 sl=30% tp=80% sf=10 cd=6 trail=15%",
    scores_v2, 80, 0.30, 0.8, 10, 6, 0.15, THETA_PER_BAR)

run("V2: entry=80 sl=30% tp=50% sf=20 cd=6 trail=15%",
    scores_v2, 80, 0.30, 0.5, 20, 6, 0.15, THETA_PER_BAR)

# 加 theta 的 V3 作為公平比較
print("\n" + "="*60)
print("V3 Night + theta (公平比較)")
print("="*60)
run("V3+theta: entry=80 sl=10% tp=100% sf=20 cd=0 trail=0",
    scores_v3, 80, 0.10, 1.0, 20, 0, 0, THETA_PER_BAR)
