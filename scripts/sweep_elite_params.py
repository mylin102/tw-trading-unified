#!/usr/bin/env python3
"""
網格回測: regime_filter × entry_score × atr_multiplier
找出這三個參數的最佳組合
"""
import sys
import numpy as np
import pandas as pd
from itertools import product
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
from strategies.futures.squeeze_futures.engine.vectorized import simulate_trades_vectorized, calculate_metrics
from rich.console import Console
from rich.table import Table

console = Console()
DATA = Path(__file__).parent.parent / "data" / "taifex_raw" / "TMF_5m_taifex.csv"

def load_data():
    df_raw = pd.read_csv(DATA, parse_dates=["ts"], index_col="ts")
    df = calculate_futures_squeeze(df_raw, bb_length=20, ema_fast=12, ema_slow=36, pb_buffer=1.002)
    # Simple MTF score proxy
    df["score"] = np.where(df["momentum"] > 0, 40, -40)
    # 15m EMA for regime filter
    df_15m = df.resample("15min").agg({"Open":"first","High":"max","Low":"min","Close":"last","Volume":"sum"}).dropna()
    df_15m["ema_filter"] = df_15m["Close"].ewm(span=12).mean()
    # Merge 15m ema back to 5m
    df["ema_filter"] = df_15m["ema_filter"].reindex(df.index, method="ffill")
    console.print(f"[green]Loaded {len(df)} bars ({df.index[0]} ~ {df.index[-1]})[/green]")
    return df

def regime_pass(row, mode):
    """Check if price passes regime filter."""
    if mode == "low":
        return True
    elif mode == "mid":
        ema = row.get("ema_filter", row["Close"])
        return row["Close"] > ema * 0.998 or row["Close"] < ema * 1.002
    else:  # high
        ema = row.get("ema_filter", row["Close"])
        return row["Close"] > ema * 0.999 or row["Close"] < ema * 1.001

def run_grid(df):
    results = []
    regimes = ["low", "mid", "high"]
    entry_scores = [10, 20, 30, 40]
    atr_mults = [1.0, 1.5, 2.0, 3.0]

    total = len(regimes) * len(entry_scores) * len(atr_mults)
    console.print(f"[cyan]Grid: {total} combinations ({len(regimes)} × {len(entry_scores)} × {len(atr_mults)})[/cyan]")

    for regime, es, atr in product(regimes, entry_scores, atr_mults):
        sqz_on = df["sqz_on"].values
        score = df["score"].values
        mom_state = df["mom_state"].values
        close = df["Close"].values
        ema = df["ema_filter"].values

        # Generate signals with regime filter
        if regime == "low":
            long_sig = (~sqz_on) & (score >= es) & (mom_state >= 2)
            short_sig = (~sqz_on) & (score <= -es) & (mom_state <= 1)
        elif regime == "mid":
            can_long = close > ema * 0.998
            can_short = close < ema * 1.002
            long_sig = (~sqz_on) & (score >= es) & (mom_state >= 2) & can_long
            short_sig = (~sqz_on) & (score <= -es) & (mom_state <= 1) & can_short
        else:  # high
            can_long = close > ema * 0.999
            can_short = close < ema * 1.001
            long_sig = (~sqz_on) & (score >= es) & (mom_state >= 2) & can_long
            short_sig = (~sqz_on) & (score <= -es) & (mom_state <= 1) & can_short

        ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
            df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
            df["vwap"].values, df["atr"].values, long_sig, short_sig,
            100000, 50, 20, 0, 0.00002, 2, 2, 1,
            stop_loss_pts=0, atr_mult=atr, tp1_pts=0, tp1_lots=0, exit_on_vwap=True,
        )
        m = calculate_metrics(pnl, ent, ext, pos, 100000)
        equity = 100000 + np.cumsum(pnl)
        dd = ((equity / np.maximum.accumulate(equity)) - 1).min() * 100

        results.append({
            "regime": regime, "entry": es, "atr_sl": atr,
            "PF": round(m["profit_factor"], 2), "Win%": round(m["win_rate"], 1),
            "PnL": round(m["total_pnl"], 0), "Trades": m["total_trades"],
            "MaxDD%": round(dd, 1),
        })

    return pd.DataFrame(results)

def main():
    console.print("[bold]🔍 網格回測: regime × entry × atr_sl[/bold]")
    df = load_data()
    results = run_grid(df)

    # Save
    out = Path(__file__).parent.parent / "exports" / "elite_param_sweep.csv"
    results.to_csv(out, index=False)
    console.print(f"[green]Saved to {out}[/green]")

    # Show results
    console.print(f"\n[bold]Top 10 by Profit Factor:[/bold]")
    for _, r in results.nlargest(10, "PF").iterrows():
        console.print(f"  PF={r['PF']:.2f} WR={r['Win%']:.1f}% regime={r['regime']:<4} entry={r['entry']} atr={r['atr_sl']}x PnL={r['PnL']:>8,.0f} DD={r['MaxDD%']:.1f}% Trades={r['Trades']:.0f}")

    # Current config
    console.print(f"\n[bold]Current config (regime=mid, entry=20, atr=1.5):[/bold]")
    cur = results[(results["regime"]=="mid") & (results["entry"]==20) & (results["atr_sl"]==1.5)]
    if not cur.empty:
        r = cur.iloc[0]
        console.print(f"  PF={r['PF']:.2f} WR={r['Win%']:.1f}% PnL={r['PnL']:,.0f} DD={r['MaxDD%']:.1f}%")

    # Best overall
    best = results.loc[results["PF"].idxmax()]
    console.print(f"\n[bold green]✨ 最佳組合:[/bold green]")
    console.print(f"  regime={best['regime']}, entry={best['entry']}, atr_sl={best['atr_sl']}x")
    console.print(f"  PF={best['PF']:.2f} WR={best['Win%']:.1f}% PnL={best['PnL']:,.0f} MaxDD={best['MaxDD%']:.1f}% Trades={best['Trades']:.0f}")

if __name__ == "__main__":
    main()
