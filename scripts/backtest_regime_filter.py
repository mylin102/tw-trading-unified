#!/usr/bin/env python3
"""
回測比較三種 regime filter 對做空的影響：
  A) mid (現行) — can_short = close < ema_filter * 1.002
  B) strict     — can_short = close < ema_filter * 1.001
  C) mid + bull_align guard — mid 條件 + bull_align 時禁止做空
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import pandas as pd
import numpy as np
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment

# ── 載入數據 ──
DATA_FILE = pathlib.Path.home() / "Documents/mylin102/tw-futures-realtime/data/taifex_raw/TMF_5m_taifex.csv"
df_raw = pd.read_csv(DATA_FILE, parse_dates=["ts"], index_col="ts")
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

# ── 對齊 15m/1h 到 5m index (forward fill) ──
df_15m_aligned = df_15m[["Close", "ema_filter", "momentum", "mom_state"]].reindex(df_5m.index, method="ffill")
df_1h_aligned = df_1h[["momentum", "mom_state"]].reindex(df_5m.index, method="ffill")

# ── 計算每根 5m bar 的 MTF score ──
scores = []
for i in range(len(df_5m)):
    data_dict = {}
    row_5m = df_5m.iloc[i]
    data_dict["5m"] = pd.DataFrame([{"momentum": row_5m["momentum"], "mom_state": row_5m["mom_state"]}])
    if not pd.isna(df_15m_aligned.iloc[i]["momentum"]):
        data_dict["15m"] = pd.DataFrame([{"momentum": df_15m_aligned.iloc[i]["momentum"], "mom_state": df_15m_aligned.iloc[i]["mom_state"]}])
    if not pd.isna(df_1h_aligned.iloc[i]["momentum"]):
        data_dict["1h"] = pd.DataFrame([{"momentum": df_1h_aligned.iloc[i]["momentum"], "mom_state": df_1h_aligned.iloc[i]["mom_state"]}])
    scores.append(calculate_mtf_alignment(data_dict)["score"])

df_5m["score"] = scores
df_5m["ema_filter_15m"] = df_15m_aligned["ema_filter"]
df_5m["close_15m"] = df_15m_aligned["Close"]

# ── 回測引擎 ──
ENTRY_SCORE = 20
STOP_LOSS_PTS = 60
POINT_VALUE = 10  # TMF 小台每點 10 TWD
FEE = 20  # 單邊手續費

def run_backtest(df, filter_name, can_short_fn):
    trades = []
    pos = 0  # 0=空手, 1=多, -1=空
    entry_price = 0

    for i in range(1, len(df)):
        row = df.iloc[i]
        close = row["Close"]

        # 出場
        if pos != 0:
            pnl_pts = (close - entry_price) * pos
            if pnl_pts <= -STOP_LOSS_PTS:
                trades.append({"direction": "LONG" if pos > 0 else "SHORT", "entry": entry_price, "exit": close, "pnl_pts": -STOP_LOSS_PTS, "reason": "STOP_LOSS"})
                pos = 0
                continue
            if pnl_pts >= STOP_LOSS_PTS:  # 簡化 TP
                trades.append({"direction": "LONG" if pos > 0 else "SHORT", "entry": entry_price, "exit": close, "pnl_pts": pnl_pts, "reason": "TP"})
                pos = 0
                continue

        # 進場
        if pos == 0:
            sqz_buy = (not row["sqz_on"]) and row["score"] >= ENTRY_SCORE and row["mom_state"] >= 2 and close > row["vwap"]
            sqz_sell = (not row["sqz_on"]) and row["score"] <= -ENTRY_SCORE and row["mom_state"] <= 1 and close < row["vwap"]

            # can_long: 用 mid filter (所有方案一樣)
            can_long = row["close_15m"] > row["ema_filter_15m"] * 0.998 if not pd.isna(row["ema_filter_15m"]) else True
            can_short = can_short_fn(row)

            if sqz_buy and can_long:
                pos = 1
                entry_price = close
            elif sqz_sell and can_short:
                pos = -1
                entry_price = close

    # 統計
    t = pd.DataFrame(trades)
    if t.empty:
        print(f"\n{'='*50}")
        print(f"[{filter_name}] 無交易")
        return t

    t["pnl_cash"] = t["pnl_pts"] * POINT_VALUE - FEE * 2
    longs = t[t["direction"] == "LONG"]
    shorts = t[t["direction"] == "SHORT"]

    print(f"\n{'='*50}")
    print(f"[{filter_name}]")
    print(f"  總交易: {len(t)}  (多:{len(longs)} 空:{len(shorts)})")
    print(f"  勝率: {(t['pnl_pts'] > 0).mean()*100:.1f}%")
    print(f"  淨損益: {t['pnl_cash'].sum():+,.0f} TWD")
    print(f"  平均每筆: {t['pnl_cash'].mean():+,.0f} TWD")
    if len(shorts) > 0:
        print(f"  --- 空單 ---")
        print(f"  空單數: {len(shorts)}")
        print(f"  空單勝率: {(shorts['pnl_pts'] > 0).mean()*100:.1f}%")
        print(f"  空單淨損益: {shorts['pnl_cash'].sum():+,.0f} TWD")
    if len(longs) > 0:
        print(f"  --- 多單 ---")
        print(f"  多單數: {len(longs)}")
        print(f"  多單勝率: {(longs['pnl_pts'] > 0).mean()*100:.1f}%")
        print(f"  多單淨損益: {longs['pnl_cash'].sum():+,.0f} TWD")
    return t


# ── A) Mid (現行) ──
def can_short_mid(row):
    if pd.isna(row["ema_filter_15m"]): return True
    return row["close_15m"] < row["ema_filter_15m"] * 1.002

# ── B) Strict ──
def can_short_strict(row):
    if pd.isna(row["ema_filter_15m"]): return True
    return row["close_15m"] < row["ema_filter_15m"] * 1.001

# ── C) Mid + bull_align guard ──
def can_short_mid_align(row):
    if row.get("bullish_align", False): return False
    if pd.isna(row["ema_filter_15m"]): return True
    return row["close_15m"] < row["ema_filter_15m"] * 1.002

print(f"\n回測期間: {df_5m.index[0].date()} ~ {df_5m.index[-1].date()}")
print(f"參數: entry_score={ENTRY_SCORE}, stop_loss={STOP_LOSS_PTS}pts")

t_a = run_backtest(df_5m, "A) Mid (現行)", can_short_mid)
t_b = run_backtest(df_5m, "B) Strict", can_short_strict)
t_c = run_backtest(df_5m, "C) Mid + bull_align guard", can_short_mid_align)
