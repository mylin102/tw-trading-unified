#!/usr/bin/env python3
"""
V1 vs V2 vs V3 options squeeze backtest using Q1 2026 TMF 5m data.

V1: daytrade — near month, theta=0.037%/bar, premium=100, force close EOD
V2: swing    — monthly,    theta=0.009%/bar, premium=250, hold overnight
V3: night    — near month, theta=0.037%/bar, premium=100, force close 04:30, bear_boost=0.8
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "strategies", "futures"))

import pandas as pd
import numpy as np
from squeeze_futures.engine.indicators import calculate_futures_squeeze, calculate_mtf_alignment

DATA = os.path.expanduser("~/Documents/mylin102/tw-option-squeeze-trading/exports/tmf_replay_5min_q1_2026.csv")
PV = 50  # TXO point value

MODE_PARAMS = {
    "V1_daytrade": {
        "theta_per_bar": 0.02 / 54,
        "initial_premium": 100,
        "force_close_hour": 13, "force_close_min": 25,
        "bear_boost": 0.0,
        "hold_overnight": False,
    },
    "V2_swing": {
        "theta_per_bar": 0.005 / 54,
        "initial_premium": 250,
        "force_close_hour": None, "force_close_min": None,
        "bear_boost": 0.0,
        "hold_overnight": True,
    },
    "V3_night": {
        "theta_per_bar": 0.02 / 54,
        "initial_premium": 100,
        "force_close_hour": 4, "force_close_min": 25,
        "bear_boost": 0.8,
        "hold_overnight": False,
    },
}


def prepare_data(path):
    df_raw = pd.read_csv(path, index_col=0, parse_dates=True)
    df_raw = df_raw[df_raw["Close"] > 20000]  # filter bad data

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
        p = {"5m": df.loc[:ts]}
        m15 = df_15m.loc[:ts]
        m1h = df_1h.loc[:ts]
        if len(m15) > 0: p["15m"] = m15
        if len(m1h) > 0: p["1h"] = m1h
        scores.append(calculate_mtf_alignment(p, weights=weights)["score"])
    df["score"] = scores
    print(f"Data: {len(df)} bars, {df.index[0]} → {df.index[-1]}")
    return df


def backtest_mode(df, mode_name, params, entry_score=80, sl_pct=0.10, tp_pct=1.0):
    theta = params["theta_per_bar"]
    premium = params["initial_premium"]
    fc_hour = params["force_close_hour"]
    fc_min = params["force_close_min"]
    bear_boost = params["bear_boost"]
    hold_overnight = params["hold_overnight"]

    pos = 0  # 0=flat, 1=holding
    side = None
    entry_p = 0
    entry_mtx = 0
    entry_bar = 0
    total_pnl = 0.0
    trades = 0
    wins = 0
    max_dd = 0
    peak_pnl = 0
    equity_curve = []

    for i in range(1, len(df)):
        row = df.iloc[i]
        price = row["Close"]
        score = row["score"]
        ts = df.index[i]
        hour, minute = ts.hour, ts.minute

        if pos == 1:
            bars_held = i - entry_bar
            pts_diff = (price - entry_mtx) * (1 if side == "C" else -1)

            # bear boost: short side gets extra delta
            delta = 0.5
            if side == "P" and bear_boost > 0:
                delta = 0.5 * (1 + bear_boost)

            cur_premium = entry_p * (1 - theta * bars_held) + pts_diff * delta

            # Force close check
            force_close = False
            if fc_hour is not None:
                if hour == fc_hour and minute >= fc_min:
                    force_close = True
                elif not hold_overnight:
                    # Day session ending
                    if fc_hour == 13 and hour >= 14:
                        force_close = True
                    # Night session ending
                    if fc_hour == 4 and hour >= 5 and hour < 8:
                        force_close = True

            # Exit logic
            exited = False
            if cur_premium <= entry_p * (1 - sl_pct):
                pnl = -sl_pct * entry_p * PV
                total_pnl += pnl; trades += 1; pos = 0; exited = True
            elif cur_premium >= entry_p * (1 + tp_pct):
                pnl = tp_pct * entry_p * PV
                total_pnl += pnl; trades += 1; wins += 1; pos = 0; exited = True
            elif abs(score) < 20:
                pnl = (cur_premium - entry_p) * PV
                total_pnl += pnl; trades += 1; pos = 0; exited = True
                if pnl > 0: wins += 1
            elif force_close:
                pnl = (cur_premium - entry_p) * PV
                total_pnl += pnl; trades += 1; pos = 0; exited = True
                if pnl > 0: wins += 1

        if pos == 0:
            # Entry — check session validity
            is_day = 8 <= hour < 14
            is_night = hour >= 15 or hour < 5

            can_enter = False
            if mode_name == "V1_daytrade" and is_day:
                can_enter = True
            elif mode_name == "V3_night" and is_night:
                can_enter = True
            elif mode_name == "V2_swing":
                can_enter = True  # swing can enter anytime

            if can_enter and not row["sqz_on"]:
                if score >= entry_score:
                    pos = 1; side = "C"; entry_p = premium; entry_mtx = price; entry_bar = i
                elif score <= -entry_score:
                    pos = 1; side = "P"; entry_p = premium; entry_mtx = price; entry_bar = i

        # Track equity
        equity_curve.append(total_pnl)
        peak_pnl = max(peak_pnl, total_pnl)
        dd = peak_pnl - total_pnl
        max_dd = max(max_dd, dd)

    # Close open position
    if pos == 1:
        pts_diff = (df.iloc[-1]["Close"] - entry_mtx) * (1 if side == "C" else -1)
        cur = entry_p * (1 - theta * (len(df) - entry_bar)) + pts_diff * 0.5
        pnl = (cur - entry_p) * PV
        total_pnl += pnl; trades += 1
        if pnl > 0: wins += 1

    wr = wins / trades * 100 if trades > 0 else 0
    avg = total_pnl / trades if trades > 0 else 0
    pf = 0
    if trades > 0:
        gross_w = sum(1 for x in equity_curve if x > 0)  # simplified

    return {
        "mode": mode_name,
        "trades": trades,
        "wins": wins,
        "win_rate": round(wr, 1),
        "total_pnl": round(total_pnl, 0),
        "avg_pnl": round(avg, 0),
        "max_dd": round(max_dd, 0),
        "equity": equity_curve,
    }


def main():
    df = prepare_data(DATA)

    print(f"\n{'Mode':<16} {'Trades':>6} {'Wins':>5} {'WR':>6} {'PnL':>10} {'Avg':>8} {'MaxDD':>8}")
    print("-" * 62)

    all_results = []
    for mode_name, params in MODE_PARAMS.items():
        r = backtest_mode(df, mode_name, params, entry_score=80, sl_pct=0.10, tp_pct=1.0)
        all_results.append(r)
        print(f"{r['mode']:<16} {r['trades']:>6} {r['wins']:>5} {r['win_rate']:>5.1f}% {r['total_pnl']:>+10,.0f} {r['avg_pnl']:>+8,.0f} {r['max_dd']:>8,.0f}")

    # Also test V2 with different params (wider SL, lower TP since holding longer)
    print("\n--- V2 Swing Variants ---")
    for sl, tp in [(0.15, 0.8), (0.20, 1.0), (0.10, 0.5)]:
        r = backtest_mode(df, f"V2_sl{int(sl*100)}_tp{int(tp*100)}", MODE_PARAMS["V2_swing"], sl_pct=sl, tp_pct=tp)
        print(f"{r['mode']:<16} {r['trades']:>6} {r['wins']:>5} {r['win_rate']:>5.1f}% {r['total_pnl']:>+10,.0f} {r['avg_pnl']:>+8,.0f} {r['max_dd']:>8,.0f}")
        all_results.append(r)

    # Save summary
    summary = pd.DataFrame([{k: v for k, v in r.items() if k != "equity"} for r in all_results])
    out = os.path.join(os.path.dirname(__file__), "..", "exports", "backtest_v1v2v3_q1_2026.csv")
    summary.to_csv(out, index=False)
    print(f"\nSaved to {out}")


if __name__ == "__main__":
    main()
