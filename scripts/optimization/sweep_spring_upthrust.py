from itertools import product
#!/usr/bin/env python3
"""
Vectorbt 回測: TTM Squeeze Spring/Upthrust (假突破反向)
對比 Counter-VWAP (等 5 根確認) vs Spring/Upthrust (即時確認)
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))
from strategies.futures.squeeze_futures.engine.vectorized import simulate_trades_vectorized, calculate_metrics
from rich.console import Console

console = Console()
EXPORTS = Path(__file__).parent.parent.parent / "exports"


def load_data():
    """載入完整 Q1 資料 (40,140 bars)"""
    csv_path = Path(__file__).parent.parent.parent / "data" / "tmf_full_2026.csv"
    if csv_path.exists():
        df = pd.read_csv(csv_path, parse_dates=True, index_col=0)
        console.print(f"[green]Loaded {len(df)} bars ({df.index[0]} ~ {df.index[-1]})[/green]")
        return df
    
    # Fallback: 合併日誌檔
    import glob
    files = sorted(glob.glob('logs/market_data/TMF_*_PAPER_indicators.csv'))
    dfs = []
    for f in files:
        try:
            df = pd.read_csv(f, parse_dates=['timestamp'])
            dfs.append(df)
        except:
            pass
    if dfs:
        df = pd.concat(dfs).sort_values('timestamp').drop_duplicates(subset=['timestamp'], keep='last')
        df.set_index('timestamp', inplace=True)
        console.print(f"[green]Loaded {len(df)} bars from logs[/green]")
        return df
    return pd.DataFrame()


def gen_spring_upthrust_signals(df, bb_len=20, bb_mult=2.0, kc_len=20, kc_mult=1.5, atr_mult=2.0):
    """
    TTM Squeeze Spring/Upthrust 信號
    - Signal Short: 前一根擠壓, 當前 K 線曾突破 BB 上軌但收盤跌回
    - Signal Long: 前一根擠壓, 當前 K 線曾跌破 BB 下軌但收盤彈回
    """
    n = len(df)
    long_sig = np.zeros(n, dtype=bool)
    short_sig = np.zeros(n, dtype=bool)
    
    # 計算 BB
    ma = df["Close"].rolling(window=bb_len).mean().values
    std = df["Close"].rolling(window=bb_len).std().values
    bb_up = ma + (bb_mult * std)
    bb_low = ma - (bb_mult * std)
    
    # 計算 KC (ATR 近似)
    prev_close = np.roll(df["Close"].values, 1)
    prev_close[0] = df["Close"].values[0]
    tr = np.maximum(df["High"].values - df["Low"].values,
                    np.maximum(np.abs(df["High"].values - prev_close),
                               np.abs(df["Low"].values - prev_close)))
    atr = np.convolve(tr, np.ones(kc_len)/kc_len, mode='full')[:n]
    
    kc_up = ma + (kc_mult * atr)
    kc_low = ma - (kc_mult * atr)
    
    # 擠壓狀態 (BB 在 KC 內)
    is_squeezing = (bb_up < kc_up) & (bb_low > kc_low)
    
    for i in range(1, n):
        if not is_squeezing[i-1]:  # 前一根必須在擠壓中
            continue
        
        # Spring (假跌破 → 做多)
        if df["Low"].iloc[i] < bb_low[i] and df["Close"].iloc[i] > bb_low[i]:
            long_sig[i] = True
        
        # Upthrust (假突破 → 做空)
        if df["High"].iloc[i] > bb_up[i] and df["Close"].iloc[i] < bb_up[i]:
            short_sig[i] = True
    
    return long_sig, short_sig


def gen_counter_vwap_signals(df, confirm_bars=5, atr_mult=2.0):
    """
    Counter-VWAP 信號 (原版: Fire 後等 5 根確認失敗)
    """
    n = len(df)
    long_sig = np.zeros(n, dtype=bool)
    short_sig = np.zeros(n, dtype=bool)
    
    fired = df["fired"].values if "fired" in df.columns else np.zeros(n, dtype=bool)
    momentum = df["momentum"].values if "momentum" in df.columns else np.zeros(n)
    vwap = df["vwap"].values if "vwap" in df.columns else df["Close"].values
    recent_high = df["recent_high"].values if "recent_high" in df.columns else df["High"].values
    recent_low = df["recent_low"].values if "recent_low" in df.columns else df["Low"].values
    mom_velo = df["mom_velo"].values if "mom_velo" in df.columns else np.zeros(n)
    
    pending_dir = 0
    fire_bar_idx = 0
    
    for i in range(1, n):
        if fired[i] and pending_dir == 0:
            pending_dir = 1 if momentum[i] > 0 else -1
            fire_bar_idx = i
            continue
        
        if pending_dir == 0:
            continue
        
        bars_since = i - fire_bar_idx
        if bars_since > confirm_bars:
            pending_dir = 0
            continue
        
        if bars_since < 1:
            continue
        
        if pending_dir == 1:  # Bullish fire failed
            no_new_high = df["Close"].iloc[i] < recent_high[i]
            velo_rev = mom_velo[i] <= 0
            vwap_rej = df["Close"].iloc[i] < vwap[i]
            if no_new_high and (velo_rev or vwap_rej):
                short_sig[i] = True
                pending_dir = 0
        else:  # Bearish fire failed
            no_new_low = df["Close"].iloc[i] > recent_low[i]
            velo_rev = mom_velo[i] >= 0
            vwap_rej = df["Close"].iloc[i] > vwap[i]
            if no_new_low and (velo_rev or vwap_rej):
                long_sig[i] = True
                pending_dir = 0
    
    return long_sig, short_sig


def run_sweep(df, strategy_name):
    """Run parameter sweep for a strategy"""
    results = []
    
    if strategy_name == "spring_upthrust":
        combos = list(product([1.5, 2.0, 2.5], [1.0, 1.5, 2.0], [1.5, 2.0, 3.0]))
        console.print(f"[cyan]Spring/Upthrust: {len(combos)} combos[/cyan]")
        
        for bb_m, kc_m, atr_m in combos:
            ls, ss = gen_spring_upthrust_signals(df, bb_mult=bb_m, kc_mult=kc_m, atr_mult=atr_m)
            if ls.sum() + ss.sum() < 2:
                continue
            ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
                df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
                df["vwap"].values, df["atr"].values, ls, ss,
                100000, 50, 20, 0, 0.00002, 1, 1, 1,
                stop_loss_pts=0, atr_mult=atr_m, tp1_pts=0, tp1_lots=0, exit_on_vwap=True,
            )
            m = calculate_metrics(pnl, ent, ext, pos, 100000)
            eq = 100000 + np.cumsum(pnl)
            dd = ((eq / np.maximum.accumulate(eq)) - 1).min() * 100 if len(eq) > 0 else 0
            results.append({"bb_mult": bb_m, "kc_mult": kc_m, "atr_sl": atr_m,
                "PF": round(m["profit_factor"], 2), "Win%": round(m["win_rate"], 1),
                "PnL": round(m["total_pnl"], 0), "Trades": int(m["total_trades"]), "MaxDD%": round(dd, 1)})

    elif strategy_name == "counter_vwap":
        combos = list(product([3, 5, 7], [1.5, 2.0, 3.0]))
        console.print(f"[cyan]Counter-VWAP: {len(combos)} combos[/cyan]")
        
        for confirm, atr_m in combos:
            ls, ss = gen_counter_vwap_signals(df, confirm_bars=confirm, atr_mult=atr_m)
            if ls.sum() + ss.sum() < 2:
                continue
            ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
                df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
                df["vwap"].values, df["atr"].values, ls, ss,
                100000, 50, 20, 0, 0.00002, 1, 1, 1,
                stop_loss_pts=0, atr_mult=atr_m, tp1_pts=0, tp1_lots=0, exit_on_vwap=True,
            )
            m = calculate_metrics(pnl, ent, ext, pos, 100000)
            eq = 100000 + np.cumsum(pnl)
            dd = ((eq / np.maximum.accumulate(eq)) - 1).min() * 100 if len(eq) > 0 else 0
            results.append({"confirm_bars": confirm, "atr_sl": atr_m,
                "PF": round(m["profit_factor"], 2), "Win%": round(m["win_rate"], 1),
                "PnL": round(m["total_pnl"], 0), "Trades": int(m["total_trades"]), "MaxDD%": round(dd, 1)})
    
    return pd.DataFrame(results)


def main():
    from itertools import product
    console.print("[bold]🔍 Vectorbt Sweep: Spring/Upthrust vs Counter-VWAP[/bold]")
    df = load_data()
    if df.empty:
        return

    console.print(f"\n[bold blue]=== Spring/Upthrust Sweep ===[/bold blue]")
    spring_r = run_sweep(df, "spring_upthrust")
    if not spring_r.empty:
        spring_r.to_csv(EXPORTS / "vbt_spring_upthrust_sweep.csv", index=False)
        console.print(f"[green]Top 5:[/green]")
        for _, r in spring_r.nlargest(5, "PF").iterrows():
            console.print(f"  PF={r['PF']:.2f} WR={r['Win%']:.1f}% BB={r['bb_mult']} KC={r['kc_mult']} ATR={r['atr_sl']}x PnL={r['PnL']:,.0f} DD={r['MaxDD%']:.1f}% T={r['Trades']}")
    else:
        console.print("[yellow]No Spring/Upthrust results[/yellow]")

    console.print(f"\n[bold blue]=== Counter-VWAP Sweep ===[/bold blue]")
    counter_r = run_sweep(df, "counter_vwap")
    if not counter_r.empty:
        counter_r.to_csv(EXPORTS / "vbt_counter_vwap_sweep_v2.csv", index=False)
        console.print(f"[green]Top 5:[/green]")
        for _, r in counter_r.nlargest(5, "PF").iterrows():
            console.print(f"  PF={r['PF']:.2f} WR={r['Win%']:.1f}% Confirm={r['confirm_bars']} ATR={r['atr_sl']}x PnL={r['PnL']:,.0f} DD={r['MaxDD%']:.1f}% T={r['Trades']}")
    else:
        console.print("[yellow]No Counter-VWAP results[/yellow]")

    console.print(f"\n{'='*60}")
    console.print("[bold]📊 Comparison: Spring/Upthrust vs Counter-VWAP[/bold]")
    console.print(f"{'='*60}")
    if not spring_r.empty:
        best_s = spring_r.loc[spring_r["PF"].idxmax()]
        console.print(f"Spring/Upthrust: PF={best_s['PF']:.2f} WR={best_s['Win%']:.1f}% PnL={best_s['PnL']:,.0f} DD={best_s['MaxDD%']:.1f}% T={int(best_s['Trades'])}")
    if not counter_r.empty:
        best_c = counter_r.loc[counter_r["PF"].idxmax()]
        console.print(f"Counter-VWAP:    PF={best_c['PF']:.2f} WR={best_c['Win%']:.1f}% PnL={best_c['PnL']:,.0f} DD={best_c['MaxDD%']:.1f}% T={int(best_c['Trades'])}")


if __name__ == "__main__":
    main()
