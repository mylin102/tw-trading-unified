#!/usr/bin/env python3
"""
Vectorbt 網格回測: PSAR Breakout + Vol-Squeeze (完整 Q1 2026 資料)
40,140 bars: 2026-01-22 ~ 2026-04-07
"""
import sys
import numpy as np
import pandas as pd
from itertools import product
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))
from strategies.futures.squeeze_futures.engine.vectorized import simulate_trades_vectorized, calculate_metrics
from rich.console import Console

console = Console()
EXPORTS = Path(__file__).parent.parent.parent / "exports"


def load_data():
    df = pd.read_csv("data/tmf_full_2026.csv", parse_dates=True, index_col=0)
    # Calculate PSAR once
    try:
        psar_df = df.ta.psar(af0=0.02, af=0.02, max_af=0.2)
        df["psar_long"] = psar_df.iloc[:, 0]
        df["psar_short"] = psar_df.iloc[:, 1]
        console.print(f"[green]PSAR calculated[/green]")
    except Exception as e:
        console.print(f"[red]PSAR failed: {e}[/red]")
        return pd.DataFrame()
    if "score" not in df.columns:
        df["score"] = np.where(df["momentum"] > 0, 40, -40)
    console.print(f"[green]Loaded {len(df)} bars ({df.index[0]} ~ {df.index[-1]})[/green]")
    return df


def gen_psar_signals(df, min_adx, sma_len):
    n = len(df)
    long_sig = np.zeros(n, dtype=bool)
    short_sig = np.zeros(n, dtype=bool)
    sma = df["Close"].rolling(sma_len).mean().values
    adx = df["adx"].values
    psar_long = df["psar_long"].values
    psar_short = df["psar_short"].values
    price = df["Close"].values

    for i in range(1, n):
        if adx[i] < min_adx:
            continue
        pl_now = not pd.isna(psar_long[i])
        pl_prev = not pd.isna(psar_long[i-1])
        ps_now = not pd.isna(psar_short[i])
        ps_prev = not pd.isna(psar_short[i-1])
        if pl_now and not pl_prev and price[i] > sma[i]:
            long_sig[i] = True
        if ps_now and not ps_prev and price[i] < sma[i]:
            short_sig[i] = True
    return long_sig, short_sig


def gen_vol_squeeze_signals(df, vol_mult, entry_score):
    n = len(df)
    long_sig = np.zeros(n, dtype=bool)
    short_sig = np.zeros(n, dtype=bool)
    vol_ma = df["Volume"].rolling(20).mean().values
    score = df["score"].values if "score" in df.columns else np.zeros(n)
    mom_state = df["mom_state"].values
    sqz_on = df["sqz_on"].values
    bull = df["bullish_align"].values
    bear = df["bearish_align"].values
    vol = df["Volume"].values
    close = df["Close"].values
    ema_filter = df["ema_filter"].values

    for i in range(1, n):
        if vol_ma[i] <= 0:
            continue
        vol_spike = vol[i] > vol_ma[i] * vol_mult
        if not vol_spike:
            continue
        can_long = bull[i] or close[i] > ema_filter[i] * 0.998
        can_short = bear[i] or close[i] < ema_filter[i] * 1.002
        if not sqz_on[i]:
            if score[i] >= entry_score and mom_state[i] >= 2 and can_long:
                long_sig[i] = True
            elif score[i] <= -entry_score and mom_state[i] <= 1 and can_short:
                short_sig[i] = True
    return long_sig, short_sig


def run_sweep(df, strategy_name):
    results = []
    if strategy_name == "psar_breakout":
        combos = list(product([10, 15, 20], [50, 100, 200], [1.5, 2.0, 3.0]))
        console.print(f"[cyan]PSAR: {len(combos)} combos[/cyan]")
        for adx, sma, atr in combos:
            ls, ss = gen_psar_signals(df, adx, sma)
            if ls.sum() + ss.sum() < 2:
                continue
            ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
                df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
                df["vwap"].values, df["atr"].values, ls, ss,
                100000, 50, 20, 0, 0.00002, 1, 1, 1,
                stop_loss_pts=0, atr_mult=atr, tp1_pts=0, tp1_lots=0, exit_on_vwap=True,
            )
            m = calculate_metrics(pnl, ent, ext, pos, 100000)
            eq = 100000 + np.cumsum(pnl)
            dd = ((eq / np.maximum.accumulate(eq)) - 1).min() * 100 if len(eq) > 0 else 0
            results.append({"min_adx": adx, "sma_len": sma, "atr_sl": atr,
                "PF": round(m["profit_factor"], 2), "Win%": round(m["win_rate"], 1),
                "PnL": round(m["total_pnl"], 0), "Trades": int(m["total_trades"]), "MaxDD%": round(dd, 1)})
    elif strategy_name == "vol_squeeze":
        combos = list(product([1.2, 1.5, 2.0], [10, 20, 30], [1.0, 1.5, 2.0]))
        console.print(f"[cyan]Vol-Squeeze: {len(combos)} combos[/cyan]")
        for vol, score, atr in combos:
            ls, ss = gen_vol_squeeze_signals(df, vol, score)
            if ls.sum() + ss.sum() < 2:
                continue
            ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
                df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
                df["vwap"].values, df["atr"].values, ls, ss,
                100000, 50, 20, 0, 0.00002, 1, 1, 1,
                stop_loss_pts=0, atr_mult=atr, tp1_pts=0, tp1_lots=0, exit_on_vwap=True,
            )
            m = calculate_metrics(pnl, ent, ext, pos, 100000)
            eq = 100000 + np.cumsum(pnl)
            dd = ((eq / np.maximum.accumulate(eq)) - 1).min() * 100 if len(eq) > 0 else 0
            results.append({"vol_mult": vol, "entry_score": score, "atr_sl": atr,
                "PF": round(m["profit_factor"], 2), "Win%": round(m["win_rate"], 1),
                "PnL": round(m["total_pnl"], 0), "Trades": int(m["total_trades"]), "MaxDD%": round(dd, 1)})
    return pd.DataFrame(results)


def main():
    console.print("[bold]🔍 Vectorbt Sweep (Full Data): PSAR + Vol-Squeeze[/bold]")
    df = load_data()
    if df.empty:
        return

    console.print(f"\n[bold blue]=== PSAR Breakout ===[/bold blue]")
    psar_r = run_sweep(df, "psar_breakout")
    if not psar_r.empty:
        psar_r.to_csv(EXPORTS / "vbt_psar_sweep_full.csv", index=False)
        console.print(f"[green]Top 5:[/green]")
        for _, r in psar_r.nlargest(5, "PF").iterrows():
            console.print(f"  PF={r['PF']:.2f} WR={r['Win%']:.1f}% ATR={r['atr_sl']}x ADX={r['min_adx']} SMA={r['sma_len']} PnL={r['PnL']:,.0f} DD={r['MaxDD%']:.1f}% T={r['Trades']}")
    else:
        console.print("[yellow]No PSAR results[/yellow]")

    console.print(f"\n[bold blue]=== Vol-Squeeze ===[/bold blue]")
    vol_r = run_sweep(df, "vol_squeeze")
    if not vol_r.empty:
        vol_r.to_csv(EXPORTS / "vbt_vol_squeeze_sweep_full.csv", index=False)
        console.print(f"[green]Top 5:[/green]")
        for _, r in vol_r.nlargest(5, "PF").iterrows():
            console.print(f"  PF={r['PF']:.2f} WR={r['Win%']:.1f}% Vol={r['vol_mult']}x Score={r['entry_score']} ATR={r['atr_sl']}x PnL={r['PnL']:,.0f} DD={r['MaxDD%']:.1f}% T={r['Trades']}")
    else:
        console.print("[yellow]No Vol-Squeeze results[/yellow]")

    console.print(f"\n{'='*60}")
    console.print("[bold]📊 Elite Strategy Comparison (Full Q1 2026)[/bold]")
    console.print(f"{'='*60}")
    counter = pd.read_csv(EXPORTS / "vbt_counter_sweep.csv")
    best_c = counter.loc[counter["PF"].idxmax()]
    console.print(f"Counter-VWAP:  PF={best_c['PF']:.2f} WR={best_c['Win%']:.1f}% PnL={best_c['PnL']:,.0f} DD={best_c['MaxDD%']:.1f}% T={int(best_c['Trades'])}")

    if not psar_r.empty:
        best_p = psar_r.loc[psar_r["PF"].idxmax()]
        console.print(f"PSAR Breakout: PF={best_p['PF']:.2f} WR={best_p['Win%']:.1f}% PnL={best_p['PnL']:,.0f} DD={best_p['MaxDD%']:.1f}% T={int(best_p['Trades'])}")
    if not vol_r.empty:
        best_v = vol_r.loc[vol_r["PF"].idxmax()]
        console.print(f"Vol-Squeeze:   PF={best_v['PF']:.2f} WR={best_v['Win%']:.1f}% PnL={best_v['PnL']:,.0f} DD={best_v['MaxDD%']:.1f}% T={int(best_v['Trades'])}")


if __name__ == "__main__":
    main()
