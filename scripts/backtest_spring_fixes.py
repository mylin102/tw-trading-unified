#!/usr/bin/env python3
"""
回測驗證 SPRING/UPTHRUST 三個修復假設：
  A) Baseline: 現行邏輯
  B) +趨勢過濾: score>0 才 SPRING, score<0 才 UPTHRUST
  C) +禁止同根K線再進場
  D) +夜盤 cooldown 加倍 (10 bars)
  E) 全部修復

用真實 TMF 5m 數據，模擬 monitor.py 的進出場邏輯。
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np
from rich.console import Console
from rich.table import Table
from strategies.futures.elite_strategies import strategy_spring_upthrust

console = Console()

# ── 載入數據 ──
def load_data():
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "tmf_full_2026.csv")
    df = pd.read_csv(path, parse_dates=["timestamp"], index_col="timestamp")
    return df

# ── 判斷夜盤 ──
def is_night(ts):
    h = ts.hour
    return h >= 15 or h < 5

# ── 回測引擎 ──
def run_backtest(df, label, trend_filter=False, no_same_bar_reentry=False, night_cooldown_mult=1, session_filter=None):
    cfg = {"strategy": {"spring_upthrust": {"bb_mult": 2.0, "kc_mult": 1.0, "atr_mult": 2.0,
                                             "bb_length": 20, "kc_length": 20}}}
    base_cooldown = 5
    cooldown_left = 0
    position = 0       # 1=long, -1=short, 0=flat
    entry_price = 0.0
    last_exit_bar = None
    trades = []
    fee_per_round = 2 * (20 + 5)  # broker + exchange × 2 sides

    for i in range(50, len(df)):
        bar = df.iloc[i]
        ts = df.index[i]
        close = bar["Close"]
        vwap = bar.get("vwap", close)
        score = bar.get("score", bar.get("momentum", 0))

        # ── 持倉管理：VWAP exit (confirm=2 模擬) ──
        if position != 0:
            vwap_violated = (position > 0 and close < vwap) or (position < 0 and close > vwap)
            if vwap_violated:
                pnl_pts = (close - entry_price) * position
                trades.append({
                    "entry_ts": entry_ts, "exit_ts": ts,
                    "side": "LONG" if position > 0 else "SHORT",
                    "entry": entry_price, "exit": close,
                    "pnl_pts": pnl_pts, "pnl_cash": pnl_pts * 10 - fee_per_round,
                    "night": is_night(entry_ts),
                    "same_bar": (entry_ts == ts),
                })
                position = 0
                entry_price = 0.0
                cd = base_cooldown * (night_cooldown_mult if is_night(ts) else 1)
                cooldown_left = cd
                last_exit_bar = ts
                continue

        # ── Cooldown ──
        if cooldown_left > 0:
            cooldown_left -= 1
            continue

        # ── 禁止同根K線再進場 ──
        if no_same_bar_reentry and last_exit_bar == ts:
            continue

        # ── Session filter ──
        if session_filter == "day" and is_night(ts):
            continue
        if session_filter == "night" and not is_night(ts):
            continue

        # ── 進場信號 ──
        if position != 0:
            continue

        state = {"last_5m": bar, "df_5m": df.iloc[:i+1],
                 "score": score, "stop_loss_pts": 60}
        signal = strategy_spring_upthrust(state, cfg)
        if not signal:
            continue

        # ── 趨勢過濾 ──
        if trend_filter:
            if signal["action"] == "BUY" and score < 0:
                continue
            if signal["action"] == "SELL" and score > 0:
                continue

        position = 1 if signal["action"] == "BUY" else -1
        entry_price = close
        entry_ts = ts

    return label, trades

# ── 統計 ──
def calc_stats(label, trades):
    if not trades:
        return {"label": label, "trades": 0, "pnl": 0, "wr": 0, "pf": 0,
                "avg_pnl": 0, "same_bar": 0, "night_trades": 0, "night_pnl": 0}
    df_t = pd.DataFrame(trades)
    wins = df_t[df_t["pnl_cash"] > 0]
    losses = df_t[df_t["pnl_cash"] <= 0]
    gross_win = wins["pnl_cash"].sum() if len(wins) else 0
    gross_loss = abs(losses["pnl_cash"].sum()) if len(losses) else 1
    night = df_t[df_t["night"]]
    return {
        "label": label,
        "trades": len(df_t),
        "pnl": df_t["pnl_cash"].sum(),
        "wr": len(wins) / len(df_t) * 100 if len(df_t) else 0,
        "pf": gross_win / gross_loss if gross_loss > 0 else float("inf"),
        "avg_pnl": df_t["pnl_cash"].mean(),
        "same_bar": df_t["same_bar"].sum(),
        "night_trades": len(night),
        "night_pnl": night["pnl_cash"].sum() if len(night) else 0,
    }

def main():
    console.print("[bold]📊 SPRING/UPTHRUST 修復假設回測[/bold]\n")
    df = load_data()
    console.print(f"數據: {df.index[0]} ~ {df.index[-1]}, {len(df)} bars\n")

    scenarios = [
        ("A) Baseline (現行)", {}),
        ("B) +趨勢過濾", {"trend_filter": True}),
        ("C) +禁同根再進場", {"no_same_bar_reentry": True}),
        ("D) +夜盤CD×2", {"night_cooldown_mult": 2}),
        ("E) 全部修復", {"trend_filter": True, "no_same_bar_reentry": True, "night_cooldown_mult": 2}),
        ("F) 僅日盤", {"session_filter": "day"}),
        ("G) 僅夜盤", {"session_filter": "night"}),
        ("H) 夜盤+趨勢過濾", {"session_filter": "night", "trend_filter": True}),
    ]

    results = []
    for label, kwargs in scenarios:
        _, trades = run_backtest(df, label, **kwargs)
        stats = calc_stats(label, trades)
        results.append(stats)

    # ── 輸出 ──
    table = Table(title="SPRING/UPTHRUST A/B 回測結果")
    table.add_column("方案", style="bold")
    table.add_column("交易數", justify="right")
    table.add_column("總PnL", justify="right")
    table.add_column("勝率%", justify="right")
    table.add_column("PF", justify="right")
    table.add_column("平均PnL", justify="right")
    table.add_column("同根進出", justify="right")
    table.add_column("夜盤筆數", justify="right")
    table.add_column("夜盤PnL", justify="right")

    for r in results:
        pnl_color = "green" if r["pnl"] > 0 else "red"
        table.add_row(
            r["label"],
            str(r["trades"]),
            f"[{pnl_color}]{r['pnl']:,.0f}[/{pnl_color}]",
            f"{r['wr']:.1f}",
            f"{r['pf']:.2f}",
            f"{r['avg_pnl']:,.0f}",
            str(r["same_bar"]),
            str(r["night_trades"]),
            f"[{'green' if r['night_pnl']>0 else 'red'}]{r['night_pnl']:,.0f}[/{'green' if r['night_pnl']>0 else 'red'}]",
        )

    console.print(table)

    # ── Baseline vs Best delta ──
    base = results[0]
    best = max(results, key=lambda x: x["pnl"])
    console.print(f"\n[bold]最佳方案: {best['label']}[/bold]")
    console.print(f"  PnL 改善: {best['pnl'] - base['pnl']:+,.0f}")
    console.print(f"  勝率改善: {best['wr'] - base['wr']:+.1f}%")
    console.print(f"  PF 改善:  {best['pf'] - base['pf']:+.2f}")

    # ── Baseline 逐筆明細 ──
    console.print("\n[bold]Baseline 逐筆交易明細:[/bold]")
    _, base_trades = run_backtest(df, "detail", **{})
    detail_table = Table(title="Baseline Trades")
    for col in ["entry_ts", "exit_ts", "side", "entry", "exit", "pnl_pts", "pnl_cash", "night"]:
        detail_table.add_column(col, justify="right" if col != "side" else "left")
    for t in base_trades:
        c = "green" if t["pnl_cash"] > 0 else "red"
        detail_table.add_row(
            str(t["entry_ts"])[:16], str(t["exit_ts"])[:16], t["side"],
            f"{t['entry']:.0f}", f"{t['exit']:.0f}",
            f"[{c}]{t['pnl_pts']:+.0f}[/{c}]", f"[{c}]{t['pnl_cash']:+,.0f}[/{c}]",
            "🌙" if t["night"] else "☀️",
        )
    console.print(detail_table)

    # ── 日盤 vs 夜盤 breakdown ──
    df_t = pd.DataFrame(base_trades)
    if len(df_t):
        day = df_t[~df_t["night"]]
        night = df_t[df_t["night"]]
        console.print(f"\n日盤: {len(day)} 筆, PnL={day['pnl_cash'].sum():+,.0f}, WR={len(day[day['pnl_cash']>0])/max(len(day),1)*100:.0f}%")
        console.print(f"夜盤: {len(night)} 筆, PnL={night['pnl_cash'].sum():+,.0f}, WR={len(night[night['pnl_cash']>0])/max(len(night),1)*100:.0f}%")

if __name__ == "__main__":
    main()
