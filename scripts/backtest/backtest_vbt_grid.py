#!/usr/bin/env python3
"""
vectorbt 網格回測：Breakout + Counter 雙模式最佳參數搜尋
使用現有 VectorizedSimulator 引擎 + vbt 分析指標/熱力圖
"""
import sys
import numpy as np
import pandas as pd
from itertools import product
from pathlib import Path

sys.path.append(str(Path(__file__).parent.parent.parent))
from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
from strategies.futures.squeeze_futures.engine.vectorized import (
    SimulatorConfig, simulate_trades_vectorized, calculate_metrics,
)
from scripts.backtest_squeeze_failure import generate_failure_signals
from rich.console import Console
from rich.table import Table

console = Console()
EXPORTS = Path(__file__).parent.parent.parent / "exports"
DATA = Path(__file__).parent.parent.parent / "data" / "taifex_raw" / "TMF_5m_taifex.csv"


def load_data():
    df_raw = pd.read_csv(DATA, parse_dates=["ts"], index_col="ts")
    df = calculate_futures_squeeze(df_raw, bb_length=20, ema_fast=12, ema_slow=36, pb_buffer=1.002)
    # Simple score proxy (real system uses MTF alignment)
    df["score"] = np.where(df["momentum"] > 0, 40, -40)
    console.print(f"[green]Loaded {len(df)} bars ({df.index[0]} ~ {df.index[-1]})[/green]")
    return df


def run_grid(df, config):
    """Run breakout parameter grid."""
    results = []
    entry_scores = [10, 20, 30, 40]
    atr_mults = [1.5, 2.0, 3.0]
    tp_pts_list = [40, 60, 80, 100]
    vwap_exits = [True, False]

    total = len(entry_scores) * len(atr_mults) * len(tp_pts_list) * len(vwap_exits)
    console.print(f"[cyan]Breakout grid: {total} combinations[/cyan]")

    for es, atr, tp, vwap in product(entry_scores, atr_mults, tp_pts_list, vwap_exits):
        sqz_on = df["sqz_on"].values
        score = df["score"].values
        mom_state = df["mom_state"].values
        long_sig = (~sqz_on) & (score >= es) & (mom_state >= 2)
        short_sig = (~sqz_on) & (score <= -es) & (mom_state <= 1)

        ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
            df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
            df["vwap"].values, df["atr"].values, long_sig, short_sig,
            config.initial_balance, config.point_value, config.fee_per_side,
            config.exchange_fee, config.tax_rate, config.max_positions,
            config.lots_per_trade, config.slippage,
            stop_loss_pts=0, atr_mult=atr, tp1_pts=tp, tp1_lots=0, exit_on_vwap=vwap,
        )
        m = calculate_metrics(pnl, ent, ext, pos, config.initial_balance)
        equity = config.initial_balance + np.cumsum(pnl)
        dd = ((equity / np.maximum.accumulate(equity)) - 1).min() * 100

        results.append({
            "entry": es, "atr_sl": atr, "tp": tp, "vwap": vwap,
            "PF": m["profit_factor"], "Win%": m["win_rate"],
            "PnL": m["total_pnl"], "Trades": m["total_trades"], "MaxDD%": round(dd, 1),
        })
    return pd.DataFrame(results)


def run_counter_grid(df, config):
    """Run counter (failure) mode parameter grid."""
    results = []
    atr_sl_mults = [0.8, 1.0, 1.5, 2.0]
    confirm_bars_list = [3, 4, 5]
    vwap_exits = [True, False]

    total = len(atr_sl_mults) * len(confirm_bars_list) * len(vwap_exits)
    console.print(f"[cyan]Counter grid: {total} combinations[/cyan]")

    for atr_sl, confirm, vwap in product(atr_sl_mults, confirm_bars_list, vwap_exits):
        long_sig, short_sig = generate_failure_signals(df, confirm)

        ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
            df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
            df["vwap"].values, df["atr"].values, long_sig, short_sig,
            config.initial_balance, config.point_value, config.fee_per_side,
            config.exchange_fee, config.tax_rate, config.max_positions,
            config.lots_per_trade, config.slippage,
            stop_loss_pts=0, atr_mult=atr_sl, tp1_pts=0, tp1_lots=0, exit_on_vwap=vwap,
        )
        m = calculate_metrics(pnl, ent, ext, pos, config.initial_balance)
        equity = config.initial_balance + np.cumsum(pnl)
        dd = ((equity / np.maximum.accumulate(equity)) - 1).min() * 100

        results.append({
            "atr_sl": atr_sl, "confirm": confirm, "vwap": vwap,
            "PF": m["profit_factor"], "Win%": m["win_rate"],
            "PnL": m["total_pnl"], "Trades": m["total_trades"], "MaxDD%": round(dd, 1),
        })
    return pd.DataFrame(results)


def print_table(title, df, cols):
    t = Table(title=title, show_lines=True)
    for c in cols:
        t.add_column(c, justify="right")
    for _, r in df.head(10).iterrows():
        t.add_row(*[f"{r[c]:.2f}" if isinstance(r[c], float) else str(r[c]) for c in cols])
    console.print(t)


def make_heatmaps(breakout_df, counter_df):
    try:
        import plotly.express as px

        # Breakout: entry_score vs atr_sl (best tp per combo)
        bp = breakout_df.groupby(["entry", "atr_sl"])["PnL"].max().reset_index()
        pivot = bp.pivot(index="entry", columns="atr_sl", values="PnL")
        fig = px.imshow(pivot, text_auto=".0f", title="Breakout PnL: Entry Score vs ATR SL",
                        color_continuous_scale="RdYlGn", labels=dict(x="ATR SL Mult", y="Entry Score"))
        fig.write_html(str(EXPORTS / "vbt_breakout_heatmap.html"))

        # Breakout: atr_sl vs tp (best entry per combo)
        bp2 = breakout_df.groupby(["atr_sl", "tp"])["PnL"].max().reset_index()
        pivot2 = bp2.pivot(index="atr_sl", columns="tp", values="PnL")
        fig2 = px.imshow(pivot2, text_auto=".0f", title="Breakout PnL: ATR SL vs TP",
                         color_continuous_scale="RdYlGn", labels=dict(x="TP pts", y="ATR SL Mult"))
        fig2.write_html(str(EXPORTS / "vbt_breakout_sl_tp_heatmap.html"))

        # Counter: atr_sl vs confirm_bars
        cp = counter_df.groupby(["atr_sl", "confirm"])["PnL"].max().reset_index()
        pivot3 = cp.pivot(index="atr_sl", columns="confirm", values="PnL")
        fig3 = px.imshow(pivot3, text_auto=".0f", title="Counter PnL: ATR SL vs Confirm Bars",
                         color_continuous_scale="RdYlGn", labels=dict(x="Confirm Bars", y="ATR SL Mult"))
        fig3.write_html(str(EXPORTS / "vbt_counter_heatmap.html"))

        console.print(f"[green]Heatmaps saved to {EXPORTS}/vbt_*_heatmap.html[/green]")
    except Exception as e:
        console.print(f"[yellow]Heatmap error: {e}[/yellow]")


def main():
    if not DATA.exists():
        console.print(f"[red]❌ {DATA} not found[/red]")
        return

    df = load_data()
    config = SimulatorConfig(point_value=10, slippage=1.0, lots_per_trade=1, max_positions=1)

    # Breakout grid
    bdf = run_grid(df, config).sort_values("PF", ascending=False)
    print_table("🔵 Breakout Top 10 (by PF)", bdf,
                ["entry", "atr_sl", "tp", "vwap", "PF", "Win%", "PnL", "Trades", "MaxDD%"])

    # Counter grid
    cdf = run_counter_grid(df, config).sort_values("PF", ascending=False)
    print_table("🔄 Counter Top 10 (by PF)", cdf,
                ["atr_sl", "confirm", "vwap", "PF", "Win%", "PnL", "Trades", "MaxDD%"])

    # Save
    bdf.to_csv(EXPORTS / "vbt_breakout_sweep.csv", index=False)
    cdf.to_csv(EXPORTS / "vbt_counter_sweep.csv", index=False)

    # Best comparison
    bb = bdf.iloc[0]
    bc = cdf.iloc[0]
    console.print(f"\n[bold]Best Breakout:[/bold]  entry={bb['entry']} atr={bb['atr_sl']} tp={bb['tp']} vwap={bb['vwap']} → PF={bb['PF']:.2f} PnL={bb['PnL']:.0f} DD={bb['MaxDD%']}%")
    console.print(f"[bold]Best Counter:[/bold]   atr={bc['atr_sl']} confirm={bc['confirm']} vwap={bc['vwap']} → PF={bc['PF']:.2f} PnL={bc['PnL']:.0f} DD={bc['MaxDD%']}%")

    # Heatmaps
    make_heatmaps(bdf, cdf)

    # vbt equity analysis on best params
    console.print("\n[bold]📊 vbt Equity Analysis (Best Breakout)[/bold]")
    sqz_on = df["sqz_on"].values
    score = df["score"].values
    mom_state = df["mom_state"].values
    long_sig = (~sqz_on) & (score >= bb["entry"]) & (mom_state >= 2)
    short_sig = (~sqz_on) & (score <= -bb["entry"]) & (mom_state <= 1)
    _, _, _, pnl, _ = simulate_trades_vectorized(
        df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
        df["vwap"].values, df["atr"].values, long_sig, short_sig,
        config.initial_balance, config.point_value, config.fee_per_side,
        config.exchange_fee, config.tax_rate, config.max_positions,
        config.lots_per_trade, config.slippage,
        stop_loss_pts=0, atr_mult=bb["atr_sl"], tp1_pts=bb["tp"], tp1_lots=0, exit_on_vwap=bool(bb["vwap"]),
    )
    equity = pd.Series(config.initial_balance + np.cumsum(pnl), index=df.index)
    returns = equity.pct_change().dropna()
    stats = {
        "Total Return": f"{(equity.iloc[-1]/equity.iloc[0]-1)*100:.1f}%",
        "Sharpe": f"{returns.mean()/returns.std()*np.sqrt(252*54):.2f}" if returns.std() > 0 else "N/A",
        "Max DD": f"{((equity/equity.cummax()-1).min())*100:.1f}%",
        "Calmar": f"{(equity.iloc[-1]/equity.iloc[0]-1)/abs((equity/equity.cummax()-1).min()):.2f}" if (equity/equity.cummax()-1).min() != 0 else "N/A",
    }
    for k, v in stats.items():
        console.print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
