#!/usr/bin/env python3
"""
選擇權參數網格優化：entry_score × stop_loss × tp1 × score_floor
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "strategies" / "options"))

import pandas as pd
import numpy as np
from itertools import product
from strategies.options.options_engine.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from strategies.options.options_engine.engine.backtest_engine import should_exit_position

# ── 載入 & 指標 ──
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

df_15m_a = df_15m[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")
df_1h_a = df_1h[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")

scores = []
for i in range(len(df_5m)):
    d = {"5m": pd.DataFrame([{"momentum": df_5m.iloc[i]["momentum"], "mom_state": df_5m.iloc[i]["mom_state"]}])}
    if not pd.isna(df_15m_a.iloc[i]["momentum"]):
        d["15m"] = pd.DataFrame([{"momentum": df_15m_a.iloc[i]["momentum"], "mom_state": df_15m_a.iloc[i]["mom_state"]}])
    if not pd.isna(df_1h_a.iloc[i]["momentum"]):
        d["1h"] = pd.DataFrame([{"momentum": df_1h_a.iloc[i]["momentum"], "mom_state": df_1h_a.iloc[i]["mom_state"]}])
    scores.append(calculate_mtf_alignment(d)["score"])
df_5m["score"] = scores

# 預轉 numpy 加速
close_arr = df_5m["Close"].values
vwap_arr = df_5m["vwap"].values
score_arr = np.array(scores)
n = len(df_5m)

POINT_VALUE = 50
DELTA = 0.5
ENTRY_PREMIUM = 100

print(f"數據: {df_5m.index[0].date()} ~ {df_5m.index[-1].date()} ({n} bars)")

# ── 回測核心 ──
def run(entry_score, sl_pct, tp1_pct, score_floor):
    pos = 0
    entry_p = 0.0
    entry_mtx = 0.0
    has_tp1 = False
    side = None
    total_pnl = 0.0
    wins = 0
    trades = 0

    for i in range(1, n):
        c = close_arr[i]
        s = score_arr[i]

        if pos > 0:
            diff = (c - entry_mtx) * (1 if side == "C" else -1)
            cur_p = entry_p + diff * DELTA

            if not has_tp1 and pos == 2 and (cur_p - entry_p) / entry_p >= tp1_pct:
                pnl = (cur_p - entry_p) * POINT_VALUE
                total_pnl += pnl
                if pnl > 0:
                    wins += 1
                trades += 1
                pos = 1
                has_tp1 = True

            if should_exit_position(cur_p, entry_p, sl_pct, s, has_tp1, score_floor=score_floor):
                pnl = (cur_p - entry_p) * POINT_VALUE * pos
                total_pnl += pnl
                if pnl > 0:
                    wins += 1
                trades += 1
                pos = 0
                continue

        if pos == 0:
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

    wr = (wins / trades * 100) if trades > 0 else 0
    avg = (total_pnl / trades) if trades > 0 else 0
    return {"entry_score": entry_score, "sl_pct": sl_pct, "tp1_pct": tp1_pct,
            "score_floor": score_floor, "trades": trades, "win_rate": wr,
            "net_pnl": total_pnl, "avg_pnl": avg}

# ── 參數網格 ──
grid = {
    "entry_score": [60, 70, 80, 90],
    "sl_pct":      [0.10, 0.15, 0.20, 0.30],
    "tp1_pct":     [0.5, 0.8, 1.0, 1.5],
    "score_floor": [0, 5, 10, 15, 20],
}

combos = list(product(*grid.values()))
print(f"參數組合: {len(combos)}")

results = []
for i, (es, sl, tp, sf) in enumerate(combos):
    results.append(run(es, sl, tp, sf))
    if (i + 1) % 100 == 0:
        print(f"  進度: {i+1}/{len(combos)}")

df_r = pd.DataFrame(results)

# ── 結果 ──
# 過濾至少 10 筆交易
df_r = df_r[df_r["trades"] >= 10].copy()
df_r = df_r.sort_values("net_pnl", ascending=False)

print(f"\n{'='*70}")
print("🏆 Top 10 (by net_pnl, trades >= 10)")
print(f"{'='*70}")
cols = ["entry_score", "sl_pct", "tp1_pct", "score_floor", "trades", "win_rate", "net_pnl", "avg_pnl"]
print(df_r[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:,.1f}"))

print(f"\n{'='*70}")
print("🏆 Top 10 (by avg_pnl, trades >= 10)")
print(f"{'='*70}")
df_r2 = df_r.sort_values("avg_pnl", ascending=False)
print(df_r2[cols].head(10).to_string(index=False, float_format=lambda x: f"{x:,.1f}"))

# score_floor 分組比較
print(f"\n{'='*70}")
print("📊 Score Floor 分組平均 (所有組合)")
print(f"{'='*70}")
grp = df_r.groupby("score_floor")[["trades", "win_rate", "net_pnl", "avg_pnl"]].mean()
print(grp.to_string(float_format=lambda x: f"{x:,.1f}"))

# 存檔
out = pathlib.Path(__file__).parent.parent / "exports" / "options_param_optimization.csv"
out.parent.mkdir(parents=True, exist_ok=True)
df_r.to_csv(out, index=False)
print(f"\n💾 結果已存: {out}")
