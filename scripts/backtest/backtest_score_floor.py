#!/usr/bin/env python3
"""
回測比較 score_floor 出場對選擇權策略的影響：
  A) score_floor=20 (現行)
  B) score_floor=10
  C) score_floor=0 (停用 score_floor 出場)

使用真實 BS 定價引擎 + MTX 5m replay 數據
"""
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "strategies" / "options"))

import pandas as pd
from strategies.options.options_engine.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment
from strategies.options.options_engine.engine.backtest_engine import (
    should_exit_position,
    stop_threshold,
)

# ── 載入數據 ──
DATA_FILE = pathlib.Path.home() / "Documents/mylin102/tw-option-squeeze-trading/exports/tmf_replay_5min_q1_2026.csv"
df_raw = pd.read_csv(DATA_FILE, parse_dates=["datetime"], index_col="datetime")
print(f"載入 {len(df_raw)} 筆 5m K 棒 ({df_raw.index[0].date()} ~ {df_raw.index[-1].date()})")

# ── 產生多週期指標 ──
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

# 對齊
df_15m_a = df_15m[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")
df_1h_a = df_1h[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")

# 計算 score
scores = []
for i in range(len(df_5m)):
    d = {"5m": pd.DataFrame([{"momentum": df_5m.iloc[i]["momentum"], "mom_state": df_5m.iloc[i]["mom_state"]}])}
    if not pd.isna(df_15m_a.iloc[i]["momentum"]):
        d["15m"] = pd.DataFrame([{"momentum": df_15m_a.iloc[i]["momentum"], "mom_state": df_15m_a.iloc[i]["mom_state"]}])
    if not pd.isna(df_1h_a.iloc[i]["momentum"]):
        d["1h"] = pd.DataFrame([{"momentum": df_1h_a.iloc[i]["momentum"], "mom_state": df_1h_a.iloc[i]["mom_state"]}])
    scores.append(calculate_mtf_alignment(d)["score"])
df_5m["score"] = scores

# ── 回測參數 ──
ENTRY_SCORE = 80
STOP_LOSS_PCT = 0.10
TP1_PCT = 1.0
POINT_VALUE = 50
DELTA = 0.5

def run_backtest(df, label, score_floor):
    trades = []
    pos = 0
    entry_price_mtx = 0
    entry_premium = 0
    has_tp1 = False
    active_side = None

    for i in range(1, len(df)):
        row = df.iloc[i]
        close = row["Close"]
        score = row["score"]

        if pos > 0:
            # 估算當前權利金
            pts_diff = (close - entry_price_mtx) * (1 if active_side == "C" else -1)
            cur_premium = entry_premium + pts_diff * DELTA

            # TP1
            if not has_tp1 and pos == 2:
                if (cur_premium - entry_premium) / entry_premium >= TP1_PCT:
                    pnl = (cur_premium - entry_premium) * POINT_VALUE
                    trades.append({"side": active_side, "entry_p": entry_premium, "exit_p": cur_premium, "pnl": pnl, "reason": "TP1", "lots": 1})
                    pos = 1
                    has_tp1 = True

            # 出場
            if should_exit_position(cur_premium, entry_premium, STOP_LOSS_PCT, score, has_tp1, score_floor=score_floor):
                pnl = (cur_premium - entry_premium) * POINT_VALUE * pos
                reason = "STOP_LOSS" if cur_premium <= stop_threshold(entry_premium, STOP_LOSS_PCT, has_tp1) else "SCORE_FLOOR"
                trades.append({"side": active_side, "entry_p": entry_premium, "exit_p": cur_premium, "pnl": pnl, "reason": reason, "lots": pos})
                pos = 0
                continue

        if pos == 0:
            # 進場: score >= entry_score → Call, score <= -entry_score → Put
            if score >= ENTRY_SCORE and close > row["vwap"]:
                active_side = "C"
            elif score <= -ENTRY_SCORE and close < row["vwap"]:
                active_side = "P"
            else:
                continue
            pos = 2
            entry_price_mtx = close
            entry_premium = 100  # 假設 ATM 權利金約 100 點
            has_tp1 = False

    # 統計
    t = pd.DataFrame(trades)
    if t.empty:
        print(f"\n{'='*55}")
        print(f"[{label}] 無交易")
        return

    total_pnl = t["pnl"].sum()
    n = len(t)
    wins = (t["pnl"] > 0).sum()
    by_reason = t.groupby("reason")["pnl"].agg(["count", "sum"])

    print(f"\n{'='*55}")
    print(f"[{label}]  score_floor={score_floor}")
    print(f"  總交易: {n}  勝率: {wins/n*100:.1f}%")
    print(f"  淨損益: {total_pnl:+,.0f} TWD  平均每筆: {total_pnl/n:+,.0f} TWD")
    print("  出場原因:")
    for reason, row in by_reason.iterrows():
        print(f"    {reason}: {int(row['count'])} 筆, {row['sum']:+,.0f} TWD")

print(f"\n回測期間: {df_5m.index[0].date()} ~ {df_5m.index[-1].date()}")
print(f"參數: entry_score={ENTRY_SCORE}, stop_loss={STOP_LOSS_PCT*100}%, tp1={TP1_PCT*100}%")

run_backtest(df_5m, "A) score_floor=20 (現行)", score_floor=20)
run_backtest(df_5m, "B) score_floor=10", score_floor=10)
run_backtest(df_5m, "C) 停用 score_floor", score_floor=0)
