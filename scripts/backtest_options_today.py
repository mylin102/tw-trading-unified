#!/usr/bin/env python3
"""
Options squeeze backtest on MXF 5m data.
Simulates ATM option buying based on squeeze score.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies", "futures"))

import pandas as pd
import numpy as np
from squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment

DATA = os.path.join(os.path.dirname(__file__), "..", "exports", "mxf_5m_full_20260401.csv")
POINT_VALUE = 50  # TXO


def run():
    df_raw = pd.read_csv(DATA, index_col=0, parse_dates=True)
    print(f"Loaded {len(df_raw)} bars: {df_raw.index[0]} → {df_raw.index[-1]}")

    df = calculate_futures_squeeze(df_raw, bb_length=20, ema_fast=20, ema_slow=60, lookback=60, pb_buffer=1.002)
    df_15m = calculate_futures_squeeze(
        df_raw.resample("15min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(),
        bb_length=20, ema_fast=20, ema_slow=60, lookback=60, pb_buffer=1.002)
    df_1h = calculate_futures_squeeze(
        df_raw.resample("1h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna(),
        bb_length=20, ema_fast=20, ema_slow=60, lookback=60, pb_buffer=1.002)

    weights = {"5m": 0.4, "15m": 0.4, "1h": 0.2}
    scores = []
    for ts in df.index:
        processed = {"5m": df.loc[:ts]}
        m15 = df_15m.loc[:ts]
        m1h = df_1h.loc[:ts]
        if len(m15) > 0: processed["15m"] = m15
        if len(m1h) > 0: processed["1h"] = m1h
        scores.append(calculate_mtf_alignment(processed, weights=weights)["score"])
    df["score"] = scores

    # Options backtest: buy ATM call/put, simulate premium with delta=0.5
    ENTRY_SCORES = [70, 80, 90, 100]
    SL_PCTS = [0.10, 0.15, 0.20, 0.30]
    TP_PCTS = [0.50, 0.80, 1.00, 1.20]
    INITIAL_PREMIUM = 100  # 假設 ATM 權利金 100 點

    results = []
    for entry_score in ENTRY_SCORES:
        for sl_pct in SL_PCTS:
            for tp_pct in TP_PCTS:
                pnl, trades, wins = _backtest_options(df, entry_score, sl_pct, tp_pct, INITIAL_PREMIUM, POINT_VALUE)
                wr = (wins / trades * 100) if trades > 0 else 0
                results.append({
                    "entry_score": entry_score,
                    "sl_pct": sl_pct,
                    "tp_pct": tp_pct,
                    "trades": trades,
                    "wins": wins,
                    "win_rate": round(wr, 1),
                    "pnl": round(pnl, 0),
                    "avg_pnl": round(pnl / trades, 0) if trades > 0 else 0,
                })

    rdf = pd.DataFrame(results).sort_values("pnl", ascending=False)
    print("\n=== Top 10 Parameter Combos (Options) ===")
    print(rdf.head(10).to_string(index=False))
    print(f"\n=== Worst 5 ===")
    print(rdf.tail(5).to_string(index=False))

    out = os.path.join(os.path.dirname(__file__), "..", "exports", "backtest_options_20260401_results.csv")
    rdf.to_csv(out, index=False)
    print(f"\nSaved {len(rdf)} combos to {out}")


def _backtest_options(df, entry_score, sl_pct, tp_pct, init_premium, pv):
    position = 0  # 0=flat, 1=holding
    side = None  # "C" or "P"
    entry_premium = 0
    entry_mtx = 0
    total_pnl = 0
    trades = 0
    wins = 0
    theta_decay = 0.02 / 54  # 每棒 theta 衰減

    for i in range(1, len(df)):
        row = df.iloc[i]
        price = row["Close"]
        score = row["score"]
        sqz_on = row["sqz_on"]

        if position == 1:
            # Mark to market: delta=0.5 linear model + theta
            pts_diff = (price - entry_mtx) * (1 if side == "C" else -1)
            cur_premium = entry_premium * (1 - theta_decay * (i - entry_bar)) + pts_diff * 0.5

            # Stop loss
            if cur_premium <= entry_premium * (1 - sl_pct):
                pnl = (entry_premium * (1 - sl_pct) - entry_premium) * pv
                total_pnl += pnl
                trades += 1
                position = 0
            # Take profit
            elif cur_premium >= entry_premium * (1 + tp_pct):
                pnl = (entry_premium * tp_pct) * pv
                total_pnl += pnl
                trades += 1
                wins += 1
                position = 0
            # Score decay exit
            elif abs(score) < 20:
                pnl = (cur_premium - entry_premium) * pv
                total_pnl += pnl
                trades += 1
                if pnl > 0: wins += 1
                position = 0
        else:
            # Entry
            if not sqz_on and score >= entry_score:
                position = 1
                side = "C"
                entry_premium = init_premium
                entry_mtx = price
                entry_bar = i
            elif not sqz_on and score <= -entry_score:
                position = 1
                side = "P"
                entry_premium = init_premium
                entry_mtx = price
                entry_bar = i

    # Close open
    if position == 1:
        pts_diff = (df.iloc[-1]["Close"] - entry_mtx) * (1 if side == "C" else -1)
        cur = entry_premium * (1 - theta_decay * (len(df) - entry_bar)) + pts_diff * 0.5
        pnl = (cur - entry_premium) * pv
        total_pnl += pnl
        trades += 1
        if pnl > 0: wins += 1

    return total_pnl, trades, wins


if __name__ == "__main__":
    run()
