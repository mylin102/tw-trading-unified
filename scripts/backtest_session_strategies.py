#!/usr/bin/env python3
"""
日盤 vs 夜盤策略選拔回測。
對所有可用策略分別在日盤/夜盤回測，找出各時段最佳策略。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from rich.console import Console
from rich.table import Table
from strategies.futures.entry_strategies import STRATEGIES
from strategies.futures.elite_strategies import strategy_spring_upthrust

console = Console()

def load_data():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "tmf_full_2026.csv")
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    return df

def is_night(ts):
    return ts.hour >= 15 or ts.hour < 5

def run_backtest(df, strategy_fn, session_filter=None):
    """Simulate: entry by strategy signal, exit by VWAP cross, cooldown=5 bars."""
    cooldown_left = 0
    position = 0
    entry_price = 0.0
    entry_ts = None
    last_exit_bar = None
    trades = []
    fee = 2 * 25  # round-trip

    cfg = {"strategy": {"spring_upthrust": {"bb_mult": 2.0, "kc_mult": 1.0,
           "atr_mult": 2.0, "bb_length": 20, "kc_length": 20}}}

    for i in range(50, len(df)):
        bar = df.iloc[i]
        ts = df.index[i]
        close = bar["Close"]
        vwap = bar.get("vwap", close)
        score = bar.get("score", bar.get("momentum", 0))

        # Session filter
        if session_filter == "day" and is_night(ts):
            # Force close at session boundary
            if position != 0:
                pnl_pts = (close - entry_price) * position
                trades.append({"pnl_pts": pnl_pts, "pnl_cash": pnl_pts * 10 - fee,
                               "entry_ts": entry_ts, "exit_ts": ts})
                position = 0
            continue
        if session_filter == "night" and not is_night(ts):
            if position != 0:
                pnl_pts = (close - entry_price) * position
                trades.append({"pnl_pts": pnl_pts, "pnl_cash": pnl_pts * 10 - fee,
                               "entry_ts": entry_ts, "exit_ts": ts})
                position = 0
            continue

        # Exit: VWAP cross
        if position != 0:
            violated = (position > 0 and close < vwap) or (position < 0 and close > vwap)
            if violated:
                pnl_pts = (close - entry_price) * position
                trades.append({"pnl_pts": pnl_pts, "pnl_cash": pnl_pts * 10 - fee,
                               "entry_ts": entry_ts, "exit_ts": ts})
                position = 0
                cooldown_left = 5
                last_exit_bar = ts
                continue

        if cooldown_left > 0:
            cooldown_left -= 1
            continue
        if last_exit_bar == ts:
            continue
        if position != 0:
            continue

        # Entry signal
        state = {"last_5m": bar, "df_5m": df.iloc[max(0,i-100):i+1],
                 "score": score, "stop_loss_pts": 60, "hour": ts.hour,
                 "fire_pending_dir": 0, "fire_bar_idx": 0,
                 "fire_high": 0, "fire_low": 0, "bar_counter": i}
        try:
            signal = strategy_fn(state, cfg)
        except Exception:
            continue
        if not signal:
            continue

        position = 1 if signal["action"] == "BUY" else -1
        entry_price = close
        entry_ts = ts

    return trades

def calc_stats(trades):
    if not trades:
        return {"n": 0, "pnl": 0, "wr": 0, "pf": 0, "avg": 0}
    df_t = pd.DataFrame(trades)
    wins = df_t[df_t["pnl_cash"] > 0]
    losses = df_t[df_t["pnl_cash"] <= 0]
    gw = wins["pnl_cash"].sum() if len(wins) else 0
    gl = abs(losses["pnl_cash"].sum()) if len(losses) else 1
    return {
        "n": len(df_t),
        "pnl": df_t["pnl_cash"].sum(),
        "wr": len(wins) / len(df_t) * 100 if len(df_t) else 0,
        "pf": gw / gl if gl > 0 else float("inf"),
        "avg": df_t["pnl_cash"].mean(),
    }

def main():
    console.print("[bold]📊 日盤 vs 夜盤 全策略回測[/bold]\n")
    df = load_data()
    console.print(f"數據: {df.index[0]} ~ {df.index[-1]}, {len(df)} bars\n")

    # All strategies to test
    all_strats = {}
    for name, info in STRATEGIES.items():
        all_strats[name] = info["func"]
    all_strats["spring_upthrust"] = strategy_spring_upthrust

    for session, label in [("day", "☀️ 日盤"), ("night", "🌙 夜盤")]:
        table = Table(title=f"{label} 策略回測 (VWAP exit, cooldown=5)")
        table.add_column("策略", style="bold")
        table.add_column("筆數", justify="right")
        table.add_column("總PnL", justify="right")
        table.add_column("勝率%", justify="right")
        table.add_column("PF", justify="right")
        table.add_column("平均PnL", justify="right")

        results = []
        for name, fn in all_strats.items():
            trades = run_backtest(df, fn, session_filter=session)
            s = calc_stats(trades)
            s["name"] = name
            results.append(s)

        # Sort by PnL
        results.sort(key=lambda x: x["pnl"], reverse=True)

        for r in results:
            c = "green" if r["pnl"] > 0 else "red"
            pf_str = f"{r['pf']:.2f}" if r["pf"] < 100 else "∞"
            table.add_row(
                r["name"], str(r["n"]),
                f"[{c}]{r['pnl']:+,.0f}[/{c}]",
                f"{r['wr']:.0f}", pf_str, f"{r['avg']:+,.0f}",
            )

        console.print(table)
        if results and results[0]["pnl"] > 0:
            console.print(f"  🏆 {label} 最佳: [bold]{results[0]['name']}[/bold] PnL={results[0]['pnl']:+,.0f} PF={results[0]['pf']:.2f}\n")
        console.print()

if __name__ == "__main__":
    main()
