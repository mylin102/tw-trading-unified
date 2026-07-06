#!/usr/bin/env python3
"""
回測比較：原始 Squeeze Breakout vs Squeeze Failure Counter (均值回歸)
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
from strategies.futures.squeeze_futures.engine.vectorized import (
    SimulatorConfig, VectorizedSimulator, simulate_trades_vectorized,
    calculate_metrics,
)
from rich.console import Console
from rich.table import Table

console = Console()

# ── Squeeze Failure signal generation ──

def generate_failure_signals(df: pd.DataFrame, confirm_bars: int = 3):
    """
    偵測 Squeeze 突破失敗後產生反向信號。
    失敗條件 (任一)：
      1. 價格未創新高/低 (多頭 fired 但 close < recent_high, 反之亦然)
      2. mom_velo 反轉歸零
      3. VWAP 拒絕 (多頭 fired 後跌破 VWAP)
    """
    n = len(df)
    long_signals = np.zeros(n, dtype=bool)
    short_signals = np.zeros(n, dtype=bool)

    fired = df["fired"].values
    momentum = df["momentum"].values
    close = df["Close"].values
    recent_high = df["recent_high"].values
    recent_low = df["recent_low"].values
    mom_velo = df["mom_velo"].values
    vwap = df["vwap"].values

    # Track pending fire events
    pending_dir = 0       # +1 = bullish fire pending, -1 = bearish
    pending_bar = -999
    fire_high = 0.0
    fire_low = 0.0

    for i in range(1, n):
        # New fire event
        if fired[i]:
            pending_dir = 1 if momentum[i] > 0 else -1
            pending_bar = i
            fire_high = close[i]
            fire_low = close[i]
            continue

        # Track extremes since fire
        if pending_dir != 0 and i > pending_bar:
            fire_high = max(fire_high, close[i])
            fire_low = min(fire_low, close[i])

        # Check failure within confirm window
        bars_since = i - pending_bar
        if pending_dir != 0 and 1 <= bars_since <= confirm_bars:
            failed = False

            if pending_dir == 1:  # Bullish fire — check failure
                no_new_high = close[i] < recent_high[i]
                velo_reversed = mom_velo[i] <= 0
                vwap_reject = close[i] < vwap[i]
                failed = no_new_high and (velo_reversed or vwap_reject)
            else:  # Bearish fire — check failure
                no_new_low = close[i] > recent_low[i]
                velo_reversed = mom_velo[i] >= 0
                vwap_reject = close[i] > vwap[i]
                failed = no_new_low and (velo_reversed or vwap_reject)

            if failed:
                # Reverse: bullish failure → short, bearish failure → long
                if pending_dir == 1:
                    short_signals[i] = True
                else:
                    long_signals[i] = True
                pending_dir = 0  # consumed

        # Expire if window passed
        if bars_since > confirm_bars:
            pending_dir = 0

    return long_signals, short_signals


# ── Failure strategy simulator (reuses core sim with custom signals + tight SL) ──

def run_failure_sim(df, config, stop_atr_mult=1.0, tp_mode="vwap", confirm_bars=3):
    long_sig, short_sig = generate_failure_signals(df, confirm_bars)
    entries, exits, positions, pnl, reasons = simulate_trades_vectorized(
        df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
        df["vwap"].values, df["atr"].values, long_sig, short_sig,
        config.initial_balance, config.point_value, config.fee_per_side,
        config.exchange_fee, config.tax_rate, config.max_positions,
        config.lots_per_trade, config.slippage,
        stop_loss_pts=0, atr_mult=stop_atr_mult,
        tp1_pts=0, tp1_lots=0,
        exit_on_vwap=(tp_mode == "vwap"),
    )
    metrics = calculate_metrics(pnl, entries, exits, positions, config.initial_balance)
    sig_count = int(long_sig.sum() + short_sig.sum())
    return metrics, sig_count


def main():
    DATA = "data/taifex_raw/TMF_5m_taifex.csv"
    if not Path(DATA).exists():
        console.print(f"[red]❌ {DATA} not found[/red]")
        return

    df_raw = pd.read_csv(DATA, parse_dates=["ts"], index_col="ts")
    df = calculate_futures_squeeze(df_raw)
    df["score"] = np.where(df["momentum"] > 0, 40, -40)
    console.print(f"[green]Loaded {len(df)} bars ({df.index[0]} ~ {df.index[-1]})[/green]")

    config = SimulatorConfig(point_value=10, slippage=1.0, lots_per_trade=1, max_positions=1)

    # ── 1. Original Breakout strategy ──
    sim = VectorizedSimulator(df, config)
    orig_results = []
    for sl in [0]:
        for atr in [2.0, 3.0]:
            for tp in [40, 80]:
                res = sim.run(entry_score=20, stop_loss_pts=sl, atr_mult=atr, tp1_pts=tp,
                              tp1_lots=0, exit_on_vwap=True)
                m = res["metrics"]
                orig_results.append({
                    "ATR": atr, "TP": tp,
                    "PF": m["profit_factor"], "Win%": m["win_rate"],
                    "PnL": m["total_pnl"], "Trades": m["total_trades"],
                    "MaxDD": m.get("max_drawdown", 0),
                })

    # ── 2. Squeeze Failure Counter strategy ──
    fail_results = []
    for atr in [0.8, 1.0, 1.5]:
        for confirm in [3, 4, 5]:
            m, sig_count = run_failure_sim(df, config, stop_atr_mult=atr,
                                           tp_mode="vwap", confirm_bars=confirm)
            fail_results.append({
                "ATR_SL": atr, "Confirm": confirm,
                "PF": m["profit_factor"], "Win%": m["win_rate"],
                "PnL": m["total_pnl"], "Trades": m["total_trades"],
                "MaxDD": m.get("max_drawdown", 0), "Signals": sig_count,
            })

    # ── Print results ──
    t1 = Table(title="Original Squeeze Breakout", show_lines=True)
    for col in ["ATR", "TP", "PF", "Win%", "PnL", "Trades", "MaxDD"]:
        t1.add_column(col, justify="right")
    for r in sorted(orig_results, key=lambda x: x["PF"], reverse=True):
        t1.add_row(*[f"{r[c]:.2f}" if isinstance(r[c], float) else str(r[c]) for c in
                      ["ATR", "TP", "PF", "Win%", "PnL", "Trades", "MaxDD"]])
    console.print(t1)

    t2 = Table(title="Squeeze Failure Counter (Mean Reversion)", show_lines=True)
    for col in ["ATR_SL", "Confirm", "PF", "Win%", "PnL", "Trades", "MaxDD", "Signals"]:
        t2.add_column(col, justify="right")
    for r in sorted(fail_results, key=lambda x: x["PF"], reverse=True):
        t2.add_row(*[f"{r[c]:.2f}" if isinstance(r[c], float) else str(r[c]) for c in
                      ["ATR_SL", "Confirm", "PF", "Win%", "PnL", "Trades", "MaxDD", "Signals"]])
    console.print(t2)

    # ── Summary ──
    best_orig = max(orig_results, key=lambda x: x["PF"])
    best_fail = max(fail_results, key=lambda x: x["PF"])
    console.print(f"\n[bold]Best Breakout:[/bold]  PF={best_orig['PF']:.2f}  Win={best_orig['Win%']:.1f}%  PnL={best_orig['PnL']:.0f}")
    console.print(f"[bold]Best Failure:[/bold]   PF={best_fail['PF']:.2f}  Win={best_fail['Win%']:.1f}%  PnL={best_fail['PnL']:.0f}")


if __name__ == "__main__":
    main()
