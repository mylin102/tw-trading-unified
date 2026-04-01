#!/usr/bin/env python3
"""
Quick vectorbt backtest on today's TMF 5m data using squeeze strategy.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies", "futures"))

import pandas as pd
import numpy as np
from squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment

DATA = os.path.join(os.path.dirname(__file__), "..", "exports", "tmf_5m_full_20260401.csv")

def run():
    df_raw = pd.read_csv(DATA, index_col=0, parse_dates=True)
    print(f"Loaded {len(df_raw)} bars: {df_raw.index[0]} → {df_raw.index[-1]}")

    # Calculate squeeze indicators
    df = calculate_futures_squeeze(df_raw, bb_length=20, ema_fast=20, ema_slow=60, lookback=60, pb_buffer=1.002)

    # Multi-timeframe: resample 15m, 1h
    df_15m = df_raw.resample("15min").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    df_1h = df_raw.resample("1h").agg({"Open": "first", "High": "max", "Low": "min", "Close": "last", "Volume": "sum"}).dropna()
    df_15m = calculate_futures_squeeze(df_15m, bb_length=20, ema_fast=20, ema_slow=60, lookback=60, pb_buffer=1.002)
    df_1h = calculate_futures_squeeze(df_1h, bb_length=20, ema_fast=20, ema_slow=60, lookback=60, pb_buffer=1.002)

    # Calculate alignment score per 5m bar
    scores = []
    weights = {"5m": 0.4, "15m": 0.4, "1h": 0.2}
    for ts in df.index:
        processed = {"5m": df.loc[:ts]}
        m15 = df_15m.loc[:ts]
        m1h = df_1h.loc[:ts]
        if len(m15) > 0:
            processed["15m"] = m15
        if len(m1h) > 0:
            processed["1h"] = m1h
        s = calculate_mtf_alignment(processed, weights=weights)["score"]
        scores.append(s)
    df["score"] = scores

    # Backtest parameters
    ENTRY_SCORES = [20, 40, 60, 80]
    SL_PTS = [40, 60, 80]
    TP_PTS = [30, 50, 80]
    POINT_VALUE = 10  # TMF

    results = []
    for entry_score in ENTRY_SCORES:
        for sl in SL_PTS:
            for tp in TP_PTS:
                pnl, trades, wins = _backtest(df, entry_score, sl, tp, POINT_VALUE)
                wr = (wins / trades * 100) if trades > 0 else 0
                results.append({
                    "entry_score": entry_score,
                    "sl_pts": sl,
                    "tp_pts": tp,
                    "trades": trades,
                    "wins": wins,
                    "win_rate": round(wr, 1),
                    "pnl": round(pnl, 0),
                    "avg_pnl": round(pnl / trades, 0) if trades > 0 else 0,
                })

    rdf = pd.DataFrame(results).sort_values("pnl", ascending=False)
    print("\n=== Top 10 Parameter Combos ===")
    print(rdf.head(10).to_string(index=False))
    print(f"\n=== Worst 5 ===")
    print(rdf.tail(5).to_string(index=False))

    out = os.path.join(os.path.dirname(__file__), "..", "exports", "backtest_20260401_results.csv")
    rdf.to_csv(out, index=False)
    print(f"\nSaved {len(rdf)} combos to {out}")


def _backtest(df, entry_score, sl_pts, tp_pts, pv):
    position = 0  # +1 long, -1 short
    entry_price = 0
    total_pnl = 0
    trades = 0
    wins = 0

    for i in range(1, len(df)):
        row = df.iloc[i]
        price = row["Close"]
        score = row["score"]
        sqz_on = row["sqz_on"]
        mom = row["mom_state"]

        if position != 0:
            # Check exit
            pnl_pts = (price - entry_price) * position
            if pnl_pts <= -sl_pts:  # stop loss
                total_pnl += -sl_pts * pv
                trades += 1
                position = 0
            elif pnl_pts >= tp_pts:  # take profit
                total_pnl += tp_pts * pv
                trades += 1
                wins += 1
                position = 0
        else:
            # Check entry
            if not sqz_on and score >= entry_score and mom >= 2:
                position = 1
                entry_price = price
            elif not sqz_on and score <= -entry_score and mom <= 1:
                position = -1
                entry_price = price

    # Close open position at last bar
    if position != 0:
        pnl_pts = (df.iloc[-1]["Close"] - entry_price) * position
        total_pnl += pnl_pts * pv
        trades += 1
        if pnl_pts > 0:
            wins += 1

    return total_pnl, trades, wins


if __name__ == "__main__":
    run()
