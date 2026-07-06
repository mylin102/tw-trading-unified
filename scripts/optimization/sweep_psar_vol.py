#!/usr/bin/env python3
"""
Vectorbt 網格回測: PSAR Breakout + Vol-Squeeze 完整掃瞄
驗證 PF 1.42 和 1.3 是否成立
"""
import sys
import numpy as np
import pandas as pd
from itertools import product
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
from strategies.futures.squeeze_futures.engine.vectorized import simulate_trades_vectorized, calculate_metrics
from strategies.futures.squeeze_futures.data.shioaji_client import ShioajiClient
from core.shioaji_session import get_api
from rich.console import Console

console = Console()
EXPORTS = Path(__file__).parent.parent.parent / "exports"


def load_data():
    """從 Shioaji API 載入 TMF 5m 數據"""
    api = get_api()
    client = ShioajiClient.__new__(ShioajiClient)
    client.api = api
    client.is_logged_in = True
    df_raw = client.get_kline("TMF", interval="5m")
    df = calculate_futures_squeeze(df_raw, bb_length=20, ema_fast=12, ema_slow=36, pb_buffer=1.002)
    # Calculate PSAR once
    try:
        psar_df = df.ta.psar(af0=0.02, af=0.02, max_af=0.2)
        df["psar_long"] = psar_df.iloc[:, 0]
        df["psar_short"] = psar_df.iloc[:, 1]
    except Exception:
        console.print("[red]PSAR calculation failed[/red]")
        return pd.DataFrame()
    
    # Simple MTF score
    df["score"] = np.where(df["momentum"] > 0, 40, -40)
    console.print(f"[green]Loaded {len(df)} bars ({df.index[0]} ~ {df.index[-1]})[/green]")
    return df


def gen_psar_signals(df, min_adx, sma_len):
    """Generate PSAR breakout signals"""
    n = len(df)
    long_sig = np.zeros(n, dtype=bool)
    short_sig = np.zeros(n, dtype=bool)
    
    sma = df["Close"].rolling(sma_len).mean().values
    
    for i in range(1, n):
        adx = df["adx"].iloc[i]
        if adx < min_adx:
            continue
        
        psar_long_now = not pd.isna(df["psar_long"].iloc[i])
        psar_long_prev = not pd.isna(df["psar_long"].iloc[i-1])
        psar_short_now = not pd.isna(df["psar_short"].iloc[i])
        psar_short_prev = not pd.isna(df["psar_short"].iloc[i-1])
        
        price = df["Close"].iloc[i]
        
        # Long: PSAR flips to long + price > SMA
        if psar_long_now and not psar_long_prev and price > sma[i]:
            long_sig[i] = True
        # Short: PSAR flips to short + price < SMA
        if psar_short_now and not psar_short_prev and price < sma[i]:
            short_sig[i] = True
    
    return long_sig, short_sig


def gen_vol_squeeze_signals(df, vol_mult, entry_score):
    """Generate Volume-Filtered Squeeze signals"""
    n = len(df)
    long_sig = np.zeros(n, dtype=bool)
    short_sig = np.zeros(n, dtype=bool)
    
    vol_ma = df["Volume"].rolling(20).mean().values
    score = df["score"].values if "score" in df.columns else np.zeros(n)
    
    for i in range(1, n):
        # Volume filter
        if vol_ma[i] <= 0:
            continue
        vol_spike = df["Volume"].iloc[i] > vol_ma[i] * vol_mult
        
        if not vol_spike:
            continue
        
        sqz_on = df["sqz_on"].iloc[i]
        mom_state = df["mom_state"].iloc[i]
        
        # Mid-regime filtering
        can_long = df["bullish_align"].iloc[i] or df["Close"].iloc[i] > df["ema_filter"].iloc[i] * 0.998
        can_short = df["bearish_align"].iloc[i] or df["Close"].iloc[i] < df["ema_filter"].iloc[i] * 1.002
        
        # Squeeze release signal
        if not sqz_on:
            if score[i] >= entry_score and mom_state >= 2 and can_long:
                long_sig[i] = True
            elif score[i] <= -entry_score and mom_state <= 1 and can_short:
                short_sig[i] = True
    
    return long_sig, short_sig


def run_sweep(df, strategy_name):
    """Run parameter sweep for a strategy"""
    results = []
    
    if strategy_name == "psar_breakout":
        # PSAR params: min_adx, sma_len, atr_mult
        adx_list = [10, 15, 20]
        sma_list = [20, 50, 100]
        atr_list = [1.5, 2.0, 3.0]
        
        total = len(adx_list) * len(sma_list) * len(atr_list)
        console.print(f"[cyan]PSAR Breakout: {total} combinations ({len(adx_list)}×{len(sma_list)}×{len(atr_list)})[/cyan]")
        
        for adx, sma, atr in product(adx_list, sma_list, atr_list):
            long_sig, short_sig = gen_psar_signals(df, adx, sma)
            total_signals = long_sig.sum() + short_sig.sum()
            
            if total_signals < 2:
                continue
            
            ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
                df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
                df["vwap"].values, df["atr"].values, long_sig, short_sig,
                100000, 50, 20, 0, 0.00002, 1, 1, 1,
                stop_loss_pts=0, atr_mult=atr, tp1_pts=0, tp1_lots=0, exit_on_vwap=True,
            )
            
            m = calculate_metrics(pnl, ent, ext, pos, 100000)
            equity = 100000 + np.cumsum(pnl)
            dd = ((equity / np.maximum.accumulate(equity)) - 1).min() * 100 if len(equity) > 0 else 0
            
            results.append({
                "min_adx": adx, "sma_len": sma, "atr_sl": atr,
                "PF": round(m["profit_factor"], 2),
                "Win%": round(m["win_rate"], 1),
                "PnL": round(m["total_pnl"], 0),
                "Trades": int(m["total_trades"]),
                "MaxDD%": round(dd, 1),
            })
    
    elif strategy_name == "vol_squeeze":
        # Vol-Squeeze params: vol_mult, entry_score, atr_mult
        vol_list = [1.2, 1.5, 2.0]
        score_list = [10, 20, 30]
        atr_list = [1.0, 1.5, 2.0]
        
        total = len(vol_list) * len(score_list) * len(atr_list)
        console.print(f"[cyan]Vol-Squeeze: {total} combinations ({len(vol_list)}×{len(score_list)}×{len(atr_list)})[/cyan]")
        
        for vol, score, atr in product(vol_list, score_list, atr_list):
            long_sig, short_sig = gen_vol_squeeze_signals(df, vol, score)
            total_signals = long_sig.sum() + short_sig.sum()
            
            if total_signals < 2:
                continue
            
            ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
                df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
                df["vwap"].values, df["atr"].values, long_sig, short_sig,
                100000, 50, 20, 0, 0.00002, 1, 1, 1,
                stop_loss_pts=0, atr_mult=atr, tp1_pts=0, tp1_lots=0, exit_on_vwap=True,
            )
            
            m = calculate_metrics(pnl, ent, ext, pos, 100000)
            equity = 100000 + np.cumsum(pnl)
            dd = ((equity / np.maximum.accumulate(equity)) - 1).min() * 100 if len(equity) > 0 else 0
            
            results.append({
                "vol_mult": vol, "entry_score": score, "atr_sl": atr,
                "PF": round(m["profit_factor"], 2),
                "Win%": round(m["win_rate"], 1),
                "PnL": round(m["total_pnl"], 0),
                "Trades": int(m["total_trades"]),
                "MaxDD%": round(dd, 1),
            })
    
    return pd.DataFrame(results)


def main():
    console.print("[bold]🔍 Vectorbt Sweep: PSAR + Vol-Squeeze[/bold]")
    df = load_data()
    if df.empty:
        return
    
    # PSAR Breakout
    console.print(f"\n[bold blue]=== PSAR Breakout Sweep ===[/bold blue]")
    psar_results = run_sweep(df, "psar_breakout")
    if not psar_results.empty:
        out = EXPORTS / "vbt_psar_sweep.csv"
        psar_results.to_csv(out, index=False)
        console.print(f"[green]Saved to {out}[/green]")
        
        console.print(f"\nTop 5 by PF:")
        for _, r in psar_results.nlargest(5, "PF").iterrows():
            console.print(f"  PF={r['PF']:.2f} WR={r['Win%']:.1f}% ATR={r['atr_sl']}x ADX={r['min_adx']} SMA={r['sma_len']} PnL={r['PnL']:,.0f} DD={r['MaxDD%']:.1f}% Trades={r['Trades']}")
    else:
        console.print("[yellow]No valid PSAR results[/yellow]")
    
    # Vol-Squeeze
    console.print(f"\n[bold blue]=== Vol-Squeeze Sweep ===[/bold blue]")
    vol_results = run_sweep(df, "vol_squeeze")
    if not vol_results.empty:
        out = EXPORTS / "vbt_vol_squeeze_sweep.csv"
        vol_results.to_csv(out, index=False)
        console.print(f"[green]Saved to {out}[/green]")
        
        console.print(f"\nTop 5 by PF:")
        for _, r in vol_results.nlargest(5, "PF").iterrows():
            console.print(f"  PF={r['PF']:.2f} WR={r['Win%']:.1f}% Vol={r['vol_mult']}x Score={r['entry_score']} ATR={r['atr_sl']}x PnL={r['PnL']:,.0f} DD={r['MaxDD%']:.1f}% Trades={r['Trades']}")
    else:
        console.print("[yellow]No valid Vol-Squeeze results[/yellow]")
    
    # Summary
    console.print(f"\n{'='*60}")
    console.print("[bold]📊 Elite Strategy Comparison (Real Backtest)[/bold]")
    console.print(f"{'='*60}")
    
    # Load all results
    counter = pd.read_csv(EXPORTS / "vbt_counter_sweep.csv")
    best_counter = counter.loc[counter["PF"].idxmax()]
    
    if not psar_results.empty:
        best_psar = psar_results.loc[psar_results["PF"].idxmax()]
        console.print(f"Counter-VWAP:  PF={best_counter['PF']:.2f} WR={best_counter['Win%']:.1f}% PnL={best_counter['PnL']:,.0f} DD={best_counter['MaxDD%']:.1f}% Trades={int(best_counter['Trades'])}")
        console.print(f"PSAR Breakout: PF={best_psar['PF']:.2f} WR={best_psar['Win%']:.1f}% PnL={best_psar['PnL']:,.0f} DD={best_psar['MaxDD%']:.1f}% Trades={int(best_psar['Trades'])}")
    else:
        console.print("PSAR Breakout: No data")
    
    if not vol_results.empty:
        best_vol = vol_results.loc[vol_results["PF"].idxmax()]
        console.print(f"Vol-Squeeze:   PF={best_vol['PF']:.2f} WR={best_vol['Win%']:.1f}% PnL={best_vol['PnL']:,.0f} DD={best_vol['MaxDD%']:.1f}% Trades={int(best_vol['Trades'])}")
    else:
        console.print("Vol-Squeeze: No data")


if __name__ == "__main__":
    main()
