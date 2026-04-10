#!/usr/bin/env python3
"""
1) cumulative_delta 夜盤深入分析
2) 日盤換 exit 策略回測 (ATR trailing stop vs VWAP vs 固定停損停利)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from rich.console import Console
from rich.table import Table
from strategies.futures.entry_strategies import STRATEGIES

console = Console()

def load_data():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "tmf_full_2026.csv")
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    return df

def is_night(ts):
    return ts.hour >= 15 or ts.hour < 5

def run_backtest(df, strategy_fn, session_filter=None, exit_mode="vwap",
                 atr_trail_mult=2.0, fixed_sl=60, fixed_tp=120):
    cooldown_left = 0
    position = 0
    entry_price = 0.0
    entry_ts = None
    last_exit_bar = None
    trail_stop = 0.0
    peak = 0.0
    trades = []
    fee = 50
    cfg = {"strategy": {"spring_upthrust": {"bb_mult": 2.0, "kc_mult": 1.0,
           "atr_mult": 2.0, "bb_length": 20, "kc_length": 20}}}

    for i in range(50, len(df)):
        bar = df.iloc[i]
        ts = df.index[i]
        close = bar["Close"]
        vwap = bar.get("vwap", close)
        atr = bar.get("atr", 50) or 50
        score = bar.get("score", bar.get("momentum", 0))

        if session_filter == "day" and is_night(ts):
            if position != 0:
                pnl_pts = (close - entry_price) * position
                trades.append(_trade(entry_ts, ts, position, entry_price, close, pnl_pts, fee, "SESSION_END"))
                position = 0
            continue
        if session_filter == "night" and not is_night(ts):
            if position != 0:
                pnl_pts = (close - entry_price) * position
                trades.append(_trade(entry_ts, ts, position, entry_price, close, pnl_pts, fee, "SESSION_END"))
                position = 0
            continue

        # Exit logic
        if position != 0:
            exit_reason = None

            if exit_mode == "vwap":
                if (position > 0 and close < vwap) or (position < 0 and close > vwap):
                    exit_reason = "VWAP"

            elif exit_mode == "atr_trail":
                if position > 0:
                    peak = max(peak, close)
                    trail_stop = peak - atr * atr_trail_mult
                    if close <= trail_stop:
                        exit_reason = "ATR_TRAIL"
                else:
                    peak = min(peak, close)
                    trail_stop = peak + atr * atr_trail_mult
                    if close >= trail_stop:
                        exit_reason = "ATR_TRAIL"

            elif exit_mode == "fixed_sltp":
                pnl = (close - entry_price) * position
                if pnl <= -fixed_sl:
                    exit_reason = "STOP_LOSS"
                elif pnl >= fixed_tp:
                    exit_reason = "TAKE_PROFIT"

            elif exit_mode == "atr_trail+vwap":
                # ATR trailing OR VWAP — whichever hits first
                if position > 0:
                    peak = max(peak, close)
                    trail_stop = peak - atr * atr_trail_mult
                    if close <= trail_stop:
                        exit_reason = "ATR_TRAIL"
                else:
                    peak = min(peak, close)
                    trail_stop = peak + atr * atr_trail_mult
                    if close >= trail_stop:
                        exit_reason = "ATR_TRAIL"
                if not exit_reason:
                    if (position > 0 and close < vwap) or (position < 0 and close > vwap):
                        exit_reason = "VWAP"

            if exit_reason:
                pnl_pts = (close - entry_price) * position
                trades.append(_trade(entry_ts, ts, position, entry_price, close, pnl_pts, fee, exit_reason))
                position = 0
                cooldown_left = 5
                last_exit_bar = ts
                continue

        if cooldown_left > 0:
            cooldown_left -= 1
            continue
        if last_exit_bar == ts or position != 0:
            continue

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
        peak = close
        trail_stop = 0.0

    return trades

def _trade(entry_ts, exit_ts, pos, entry, exit_, pnl_pts, fee, reason):
    return {"entry_ts": entry_ts, "exit_ts": exit_ts,
            "side": "LONG" if pos > 0 else "SHORT",
            "entry": entry, "exit": exit_,
            "pnl_pts": pnl_pts, "pnl_cash": pnl_pts * 10 - fee,
            "reason": reason, "night": is_night(entry_ts),
            "hold_bars": int((exit_ts - entry_ts).total_seconds() / 300)}

def calc_stats(trades):
    if not trades:
        return {"n": 0, "pnl": 0, "wr": 0, "pf": 0, "avg": 0, "max_dd": 0}
    df_t = pd.DataFrame(trades)
    wins = df_t[df_t["pnl_cash"] > 0]
    losses = df_t[df_t["pnl_cash"] <= 0]
    gw = wins["pnl_cash"].sum() if len(wins) else 0
    gl = abs(losses["pnl_cash"].sum()) if len(losses) else 1
    eq = df_t["pnl_cash"].cumsum()
    dd = (eq - eq.cummax()).min()
    return {
        "n": len(df_t), "pnl": df_t["pnl_cash"].sum(),
        "wr": len(wins) / len(df_t) * 100,
        "pf": gw / gl if gl > 0 else float("inf"),
        "avg": df_t["pnl_cash"].mean(), "max_dd": dd,
    }

def main():
    df = load_data()

    # ═══════════════════════════════════════════
    # Part 1: cumulative_delta 夜盤深入分析
    # ═══════════════════════════════════════════
    console.print("[bold]═══ Part 1: cumulative_delta 夜盤分析 ═══[/bold]\n")
    cd_fn = STRATEGIES["cumulative_delta"]["func"]
    trades = run_backtest(df, cd_fn, session_filter="night")
    df_t = pd.DataFrame(trades)

    if len(df_t):
        # Monthly breakdown
        df_t["month"] = pd.to_datetime(df_t["entry_ts"]).dt.to_period("M")
        monthly = df_t.groupby("month").agg(
            n=("pnl_cash", "count"),
            pnl=("pnl_cash", "sum"),
            wr=("pnl_cash", lambda x: (x > 0).mean() * 100),
            avg=("pnl_cash", "mean"),
        )
        console.print("[bold]月度分佈:[/bold]")
        console.print(monthly.to_string())

        # Win/loss distribution
        console.print(f"\n[bold]PnL 分佈:[/bold]")
        console.print(f"  最大單筆獲利: {df_t['pnl_cash'].max():+,.0f}")
        console.print(f"  最大單筆虧損: {df_t['pnl_cash'].min():+,.0f}")
        console.print(f"  中位數: {df_t['pnl_cash'].median():+,.0f}")
        console.print(f"  標準差: {df_t['pnl_cash'].std():,.0f}")

        # Top 5 / Bottom 5
        console.print(f"\n[bold]Top 5 獲利:[/bold]")
        for _, r in df_t.nlargest(5, "pnl_cash").iterrows():
            console.print(f"  {r['entry_ts']} {r['side']} +{r['pnl_cash']:,.0f} hold={r['hold_bars']}bars")
        console.print(f"\n[bold]Top 5 虧損:[/bold]")
        for _, r in df_t.nsmallest(5, "pnl_cash").iterrows():
            console.print(f"  {r['entry_ts']} {r['side']} {r['pnl_cash']:,.0f} hold={r['hold_bars']}bars")

        # Holding time distribution
        console.print(f"\n[bold]持倉時間:[/bold]")
        console.print(f"  平均: {df_t['hold_bars'].mean():.1f} bars ({df_t['hold_bars'].mean()*5:.0f} min)")
        console.print(f"  中位: {df_t['hold_bars'].median():.0f} bars")
        console.print(f"  最長: {df_t['hold_bars'].max()} bars")

        # Consecutive losses
        streaks = []
        cur = 0
        for p in df_t["pnl_cash"]:
            if p <= 0:
                cur += 1
            else:
                if cur > 0: streaks.append(cur)
                cur = 0
        if cur > 0: streaks.append(cur)
        console.print(f"  最大連虧: {max(streaks) if streaks else 0} 筆")

        # Equity curve drawdown
        eq = df_t["pnl_cash"].cumsum()
        dd = (eq - eq.cummax()).min()
        console.print(f"  最大回撤: {dd:,.0f}")

    # ═══════════════════════════════════════════
    # Part 2: 日盤換 exit 策略
    # ═══════════════════════════════════════════
    console.print("\n[bold]═══ Part 2: 日盤換 exit 策略 ═══[/bold]\n")

    # Test top day-session entry strategies with different exits
    day_entries = ["cumulative_delta", "vwap_bounce", "spring_upthrust", "volume_reversal"]
    exit_modes = [
        ("vwap", "VWAP exit", {}),
        ("atr_trail", "ATR Trail 2x", {"atr_trail_mult": 2.0}),
        ("atr_trail", "ATR Trail 3x", {"atr_trail_mult": 3.0}),
        ("fixed_sltp", "SL60/TP120", {"fixed_sl": 60, "fixed_tp": 120}),
        ("fixed_sltp", "SL80/TP200", {"fixed_sl": 80, "fixed_tp": 200}),
        ("atr_trail+vwap", "ATR2x+VWAP", {"atr_trail_mult": 2.0}),
    ]

    table = Table(title="日盤: 進場策略 × 出場策略")
    table.add_column("進場", style="bold")
    table.add_column("出場")
    table.add_column("筆數", justify="right")
    table.add_column("PnL", justify="right")
    table.add_column("WR%", justify="right")
    table.add_column("PF", justify="right")
    table.add_column("Avg", justify="right")
    table.add_column("MaxDD", justify="right")

    for entry_name in day_entries:
        fn = STRATEGIES[entry_name]["func"] if entry_name != "spring_upthrust" else \
             __import__("strategies.futures.elite_strategies", fromlist=["strategy_spring_upthrust"]).strategy_spring_upthrust
        for exit_mode, exit_label, exit_kw in exit_modes:
            trades = run_backtest(df, fn, session_filter="day",
                                 exit_mode=exit_mode, **exit_kw)
            s = calc_stats(trades)
            c = "green" if s["pnl"] > 0 else "red"
            pf_str = f"{s['pf']:.2f}" if s["pf"] < 100 else "∞"
            table.add_row(
                entry_name, exit_label, str(s["n"]),
                f"[{c}]{s['pnl']:+,.0f}[/{c}]",
                f"{s['wr']:.0f}", pf_str, f"{s['avg']:+,.0f}",
                f"{s['max_dd']:,.0f}",
            )

    console.print(table)

if __name__ == "__main__":
    main()
