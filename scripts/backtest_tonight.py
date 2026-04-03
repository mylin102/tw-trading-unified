#!/usr/bin/env python3
"""
今晚夜盤 (20260402) vectorbt 回測：用 indicator CSV 重建信號，掃描最佳參數
"""
import sys
import numpy as np
import pandas as pd
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent))
from strategies.futures.squeeze_futures.engine.indicators import calculate_atr
from strategies.futures.squeeze_futures.engine.vectorized import (
    SimulatorConfig, simulate_trades_vectorized, calculate_metrics,
)
from scripts.backtest_squeeze_failure import generate_failure_signals
from rich.console import Console
from rich.table import Table
from itertools import product

console = Console()
EXPORTS = Path(__file__).parent.parent / "exports"
CSV = Path(__file__).parent.parent / "logs" / "market_data" / "TMF_20260402_PAPER_indicators.csv"


def load_tonight():
    """Load tonight's indicator CSV, recalculate missing columns (atr, mom_velo)."""
    df = pd.read_csv(CSV, parse_dates=["timestamp"], index_col="timestamp")
    # Filter night session only (15:00~05:00)
    night = df[df.index.hour >= 15] 
    early = df[df.index.hour < 5]
    df = pd.concat([night, early]).sort_index()

    if df.empty:
        console.print("[red]No night session data[/red]")
        sys.exit(1)

    # Recalculate atr and mom_velo from OHLCV
    df["atr"] = calculate_atr(df, length=20)
    df["mom_velo"] = df["momentum"].diff(1).rolling(window=3).mean().fillna(0.0)

    # Ensure boolean columns
    for col in ["sqz_on", "fired", "bullish_align", "bearish_align"]:
        if col in df.columns:
            df[col] = df[col].astype(bool)

    # Fill NaN
    df["Volume"] = df["Volume"].fillna(0)
    df["atr"] = df["atr"].fillna(0)
    df["vwap"] = df["vwap"].fillna(df["Close"])

    console.print(f"[green]Loaded {len(df)} night bars ({df.index[0]} ~ {df.index[-1]})[/green]")
    console.print(f"  Price range: {df['Close'].min():.0f} ~ {df['Close'].max():.0f}")
    console.print(f"  ATR mean: {df['atr'].mean():.1f}")
    return df


def run_breakout_grid(df, config):
    results = []
    entry_scores = [10, 20, 30, 40]
    atr_mults = [1.5, 2.0, 3.0, 4.0]
    vwap_exits = [True, False]

    df["score"] = df.get("score", df["momentum"].apply(lambda x: 40 if x > 0 else -40))
    score = df["score"].values
    sqz_on = df["sqz_on"].values
    mom_state = df["mom_state"].astype(int).values

    for es, atr, vwap in product(entry_scores, atr_mults, vwap_exits):
        long_sig = (~sqz_on) & (score >= es) & (mom_state >= 2)
        short_sig = (~sqz_on) & (score <= -es) & (mom_state <= 1)

        _, _, _, pnl, _ = simulate_trades_vectorized(
            df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
            df["vwap"].values, df["atr"].values, long_sig, short_sig,
            config.initial_balance, config.point_value, config.fee_per_side,
            config.exchange_fee, config.tax_rate, config.max_positions,
            config.lots_per_trade, config.slippage,
            stop_loss_pts=0, atr_mult=atr, tp1_pts=0, tp1_lots=0, exit_on_vwap=vwap,
        )
        m = calculate_metrics(pnl, np.zeros(len(pnl)), np.zeros(len(pnl)), np.zeros(len(pnl)), config.initial_balance)
        equity = config.initial_balance + np.cumsum(pnl)
        dd = ((equity / np.maximum.accumulate(equity)) - 1).min() * 100 if len(equity) > 0 else 0

        results.append({
            "entry": es, "atr_sl": atr, "vwap": vwap,
            "PF": m["profit_factor"], "Win%": m["win_rate"],
            "PnL": m["total_pnl"], "Trades": m["total_trades"], "MaxDD%": round(dd, 1),
        })
    return pd.DataFrame(results)


def run_counter_grid(df, config):
    results = []
    atr_sl_mults = [1.0, 1.5, 2.0, 3.0]
    confirm_bars_list = [3, 5, 7]
    vwap_exits = [True, False]

    for atr_sl, confirm, vwap in product(atr_sl_mults, confirm_bars_list, vwap_exits):
        long_sig, short_sig = generate_failure_signals(df, confirm)

        _, _, _, pnl, _ = simulate_trades_vectorized(
            df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
            df["vwap"].values, df["atr"].values, long_sig, short_sig,
            config.initial_balance, config.point_value, config.fee_per_side,
            config.exchange_fee, config.tax_rate, config.max_positions,
            config.lots_per_trade, config.slippage,
            stop_loss_pts=0, atr_mult=atr_sl, tp1_pts=0, tp1_lots=0, exit_on_vwap=vwap,
        )
        m = calculate_metrics(pnl, np.zeros(len(pnl)), np.zeros(len(pnl)), np.zeros(len(pnl)), config.initial_balance)
        equity = config.initial_balance + np.cumsum(pnl)
        dd = ((equity / np.maximum.accumulate(equity)) - 1).min() * 100 if len(equity) > 0 else 0

        results.append({
            "atr_sl": atr_sl, "confirm": confirm, "vwap": vwap,
            "PF": m["profit_factor"], "Win%": m["win_rate"],
            "PnL": m["total_pnl"], "Trades": m["total_trades"], "MaxDD%": round(dd, 1),
        })
    return pd.DataFrame(results)


def print_table(title, df_res, cols):
    t = Table(title=title, show_lines=True)
    for c in cols:
        t.add_column(c, justify="right")
    for _, r in df_res.head(10).iterrows():
        t.add_row(*[f"{r[c]:.2f}" if isinstance(r[c], float) else str(r[c]) for c in cols])
    console.print(t)


def main():
    if not CSV.exists():
        console.print(f"[red]❌ {CSV} not found[/red]")
        return

    df = load_tonight()
    config = SimulatorConfig(
        point_value=10, fee_per_side=20, exchange_fee=0,
        tax_rate=0.00002, slippage=1.0, lots_per_trade=2, max_positions=2,
    )

    # Breakout grid
    console.print("\n[bold cyan]═══ Breakout Grid ═══[/bold cyan]")
    bdf = run_breakout_grid(df, config).sort_values("PnL", ascending=False)
    print_table("🔵 Breakout Top 10 (by PnL)", bdf,
                ["entry", "atr_sl", "vwap", "PF", "Win%", "PnL", "Trades", "MaxDD%"])

    # Counter grid
    console.print("\n[bold cyan]═══ Counter Grid ═══[/bold cyan]")
    cdf = run_counter_grid(df, config).sort_values("PnL", ascending=False)
    print_table("🔄 Counter Top 10 (by PnL)", cdf,
                ["atr_sl", "confirm", "vwap", "PF", "Win%", "PnL", "Trades", "MaxDD%"])

    # Save
    bdf.to_csv(EXPORTS / "tonight_breakout_sweep.csv", index=False)
    cdf.to_csv(EXPORTS / "tonight_counter_sweep.csv", index=False)

    # Compare with actual trades
    console.print("\n[bold yellow]═══ 今晚實際交易 vs 最佳回測 ═══[/bold yellow]")
    actual_csv = Path(__file__).parent.parent / "exports" / "trades" / "TMF_20260402_trades.csv"
    if actual_csv.exists():
        actual = pd.read_csv(actual_csv)
        exits = actual[actual["type"] == "EXIT"]
        actual_pnl = exits["pnl_cash"].sum()
        actual_trades = len(exits)
        console.print(f"  實際: {actual_trades} trades, PnL = {actual_pnl:.0f} 元 (未扣手續費)")

    if not bdf.empty:
        bb = bdf.iloc[0]
        console.print(f"  最佳 Breakout: entry={bb['entry']} atr={bb['atr_sl']} vwap={bb['vwap']} → {bb['Trades']:.0f} trades, PnL={bb['PnL']:.0f}")
    if not cdf.empty:
        bc = cdf.iloc[0]
        console.print(f"  最佳 Counter:  atr={bc['atr_sl']} confirm={bc['confirm']} vwap={bc['vwap']} → {bc['Trades']:.0f} trades, PnL={bc['PnL']:.0f}")


if __name__ == "__main__":
    main()
