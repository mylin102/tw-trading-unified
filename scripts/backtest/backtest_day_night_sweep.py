#!/usr/bin/env python3
"""
日盤 vs 夜盤 策略參數掃描 (v2)
- 使用 signal_generator.py (正確的 Counter-VWAP fire state tracking)
- 使用 vectorized simulator (Numba 加速，含摩擦成本)
- 對 elite strategies 做日夜盤分離參數掃描
- 修復: 正確停損設定、去重複行、Spring/Upthrust 支援

Usage:
    python scripts/backtest/backtest_day_night_sweep.py          # 全掃描
    python scripts/backtest/backtest_day_night_sweep.py --quick  # 快速模式
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))

import argparse
import numpy as np
import pandas as pd
from itertools import product
from pathlib import Path

from rich.console import Console
from rich.table import Table

from backtest.signal_generator import generate_signals
from strategies.futures.squeeze_futures.engine.vectorized import (
    simulate_trades_vectorized, calculate_metrics,
)
from strategies.futures.squeeze_futures.engine.indicators import (
    calculate_futures_squeeze,
)
from strategies.futures.elite_strategies import ELITE_STRATEGIES

console = Console()
EXPORTS = Path(__file__).parent.parent.parent / "exports"
DATA_PATH = Path(__file__).parent.parent.parent / "data" / "tmf_full_2026.csv"

# ── Session classification ──
def is_night(ts):
    """Same logic as core.date_utils.is_night_session."""
    h = ts.hour if hasattr(ts, 'hour') else ts
    return h >= 15 or h < 5

def build_eod_bars(df):
    """Mark bars that are the last bar of a trading day."""
    eod_bars = np.zeros(len(df), dtype=np.bool_)
    if "trading_day" not in df.columns:
        return eod_bars
    td = df["trading_day"].values
    for i in range(1, len(td)):
        if td[i] != td[i-1]:
            eod_bars[i-1] = True
    return eod_bars

# ── Load & split data ──
def load_data():
    if not DATA_PATH.exists():
        console.print(f"[red]Data not found: {DATA_PATH}[/red]")
        sys.exit(1)

    df = pd.read_csv(DATA_PATH, parse_dates=["timestamp"], index_col="timestamp")

    # Ensure indicators
    if "vwap" not in df.columns:
        console.print("[yellow]Calculating VWAP + ATR + squeeze indicators...[/yellow]")
        df = calculate_futures_squeeze(df)

    # Session split preserving index continuity
    session_flags = df.index.to_series().apply(is_night).values
    day_mask = ~session_flags
    night_mask = session_flags

    # Build session DataFrames (subset of original, preserving indices)
    df_day = df[day_mask].copy()
    df_night = df[night_mask].copy()

    console.print(f"[green]Loaded {len(df)} bars: {df.index[0]} ~ {df.index[-1]}[/green]")
    console.print(f"  ☀️ 日盤: {len(df_day)} bars ({df_day.index[0]} ~ {df_day.index[-1]})")
    console.print(f"  🌙 夜盤: {len(df_night)} bars ({df_night.index[0]} ~ {df_night.index[-1]})")

    return df, df_day, df_night

# ── Single backtest ──
def run_single(strategy_name, df, cfg):
    """Run one strategy on one session, return metrics dict."""
    # Generate signals
    long_sig, short_sig = generate_signals(df, strategy_name, cfg)
    if not long_sig.any() and not short_sig.any():
        return None

    eod = build_eod_bars(df)

    # Determine stop loss and ATR mult based on strategy
    strat_cfg = cfg.get("strategy", {})
    if strategy_name == "counter_vwap":
        cm = strat_cfg.get("counter_mode", {})
        atr = df["atr"].values
        avg_atr = np.mean(atr[atr > 0]) if np.any(atr > 0) else 30
        stop_loss_pts = cm.get("atr_sl_mult", 2.0) * avg_atr
        atr_mult_val = 0  # Use fixed stop_loss_pts
    elif strategy_name == "spring_upthrust":
        su = strat_cfg.get("spring_upthrust", {})
        atr = df["atr"].values
        avg_atr = np.mean(atr[atr > 0]) if np.any(atr > 0) else 30
        stop_loss_pts = su.get("atr_mult", 2.0) * avg_atr
        atr_mult_val = 0
    else:
        stop_loss_pts = 60
        atr_mult_val = 0

    ent, ext, pos, pnl, reasons = simulate_trades_vectorized(
        df["Open"].values, df["Close"].values, df["High"].values, df["Low"].values,
        df["vwap"].values, df["atr"].values,
        long_sig, short_sig,
        cfg["initial_balance"], cfg["point_value"], cfg["fee_per_side"],
        cfg["exchange_fee"], cfg["tax_rate"], cfg["max_positions"],
        cfg["lots_per_trade"], cfg["slippage"],
        stop_loss_pts=stop_loss_pts, atr_mult=atr_mult_val,
        tp1_pts=0, tp1_lots=0, exit_on_vwap=True,
        intraday_only=False, eod_bars=eod,
    )
    m = calculate_metrics(pnl, ent, ext, pos, cfg["initial_balance"])
    equity = cfg["initial_balance"] + np.cumsum(pnl)
    peak = np.maximum.accumulate(equity)
    dd = ((equity / peak) - 1).min() * 100 if len(equity) > 0 else 0

    return {
        "profit_factor": m["profit_factor"],
        "win_rate": m["win_rate"],
        "total_pnl": m["total_pnl"],
        "total_trades": int(m["total_trades"]),
        "max_dd": round(dd, 1),
    }

# ── Parameter sweep ──
SWEEP_PARAMS = {
    "counter_vwap": {
        "atr_sl_mult": [1.0, 1.5, 2.0, 2.5, 3.0],
        "confirm_bars": [3, 4, 5, 6, 7],
    },
    "spring_upthrust": {
        "atr_mult": [1.5, 2.0, 2.5, 3.0],
    },
}

def sweep_strategy(strategy_name, df, base_cfg, sweep_params):
    """Sweep parameters, return deduplicated DataFrame."""
    results = []
    keys = list(sweep_params.keys())
    values = [sweep_params[k] for k in keys]
    total = 1
    for v in values:
        total *= len(v)

    console.print(f"  Sweeping {total} combinations...")

    for combo in product(*values):
        cfg = _apply_params(base_cfg, strategy_name, dict(zip(keys, combo)))
        res = run_single(strategy_name, df, cfg)
        if res:
            row = {k: v for k, v in zip(keys, combo)}
            row.update(res)
            results.append(row)

    if not results:
        return pd.DataFrame()

    df_res = pd.DataFrame(results)
    # Deduplicate: round floats + drop exact duplicates
    float_cols = df_res.select_dtypes(include=['float64']).columns
    for c in float_cols:
        df_res[c] = df_res[c].round(2)
    df_res = df_res.drop_duplicates(subset=list(df_res.columns))
    # Also sort by PF desc, then by total_pnl desc
    return df_res.sort_values(
        ["profit_factor", "total_pnl"], ascending=[False, False]
    ).reset_index(drop=True)

def _apply_params(base_cfg, strategy_name, params):
    """Apply sweep params to config."""
    import copy
    cfg = copy.deepcopy(base_cfg)
    for k, v in params.items():
        if strategy_name == "counter_vwap":
            cfg.setdefault("strategy", {}).setdefault("counter_mode", {})[k] = v
        elif strategy_name == "spring_upthrust":
            cfg.setdefault("strategy", {}).setdefault("spring_upthrust", {})[k] = v
    return cfg

# ── Display table (deduplicated) ──
def show_top(df_res, strategy_name, session_label, n=5):
    """Show top N unique results."""
    if df_res.empty:
        return

    # Select display columns (params + results)
    param_cols = [c for c in df_res.columns if c not in (
        "profit_factor", "win_rate", "total_pnl", "total_trades", "max_dd")]
    display_cols = param_cols + ["profit_factor", "win_rate", "total_pnl", "total_trades", "max_dd"]

    top = df_res.head(n)
    table = Table(title=f"Top {n} — {strategy_name} / {session_label}")
    for col in display_cols:
        justify = "right"
        table.add_column(col, justify=justify)

    for _, row in top.iterrows():
        vals = []
        for c in display_cols:
            v = row[c]
            if isinstance(v, (int, np.integer)):
                vals.append(str(v))
            elif isinstance(v, float):
                if c == "profit_factor":
                    vals.append(f"{v:.2f}")
                elif c in ("win_rate", "max_dd"):
                    vals.append(f"{v:.1f}")
                elif c == "total_pnl":
                    vals.append(f"{v:+,.0f}")
                else:
                    vals.append(f"{v:.0f}")
            else:
                vals.append(str(v))
        table.add_row(*vals)
    console.print(table)

# ── Main ──
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quick", action="store_true", help="Quick mode: fewer param combos")
    parser.add_argument("--strategy", type=str, default=None, help="Test specific strategy only")
    args = parser.parse_args()

    console.print("[bold]📊 日盤 vs 夜盤 策略參數掃描 (v2)[/bold]\n")

    df_full, df_day, df_night = load_data()

    strategies = {args.strategy: ELITE_STRATEGIES[args.strategy]} if args.strategy else ELITE_STRATEGIES

    if args.quick:
        SWEEP_PARAMS["counter_vwap"] = {
            "atr_sl_mult": [1.5, 2.0, 2.5],
            "confirm_bars": [3, 5, 7],
        }
        SWEEP_PARAMS["spring_upthrust"] = {
            "atr_mult": [2.0, 2.5, 3.0],
        }

    all_results = []

    for strat_name, strat_info in strategies.items():
        console.print(f"\n{'='*60}")
        console.print(f"🔬 {strat_name} (elite rank #{strat_info.get('elite_rank', '?')})")
        console.print(f"   文獻 PF={strat_info.get('backtest_pf', '?')}, "
                      f"WR={strat_info.get('backtest_wr', '?')}%, "
                      f"MaxDD={strat_info.get('backtest_maxdd', '?')}%")
        console.print(f"{'='*60}")

        base_cfg = {
            "initial_balance": 100000,
            "point_value": 50,
            "fee_per_side": 20,
            "exchange_fee": 0,
            "tax_rate": 0.00002,
            "max_positions": 1,
            "lots_per_trade": 1,
            "slippage": 1,
        }
        if strat_name == "counter_vwap":
            base_cfg["strategy"] = {"counter_mode": {"enabled": True, "confirm_bars": 5, "atr_sl_mult": 2.0}}
        elif strat_name == "spring_upthrust":
            base_cfg["strategy"] = {"spring_upthrust": {"bb_mult": 2.0, "kc_mult": 1.0, "atr_mult": 2.0}}

        sweep = SWEEP_PARAMS.get(strat_name, {})

        for session_label, session_df in [("日盤 ☀️", df_day), ("夜盤 🌙", df_night), ("全天", df_full)]:
            console.print(f"\n  {session_label} ({len(session_df)} bars)")

            if sweep:
                df_res = sweep_strategy(strat_name, session_df, base_cfg, sweep)
            else:
                res = run_single(strat_name, session_df, base_cfg)
                if res:
                    df_res = pd.DataFrame([{**res}])
                else:
                    df_res = pd.DataFrame()

            if df_res.empty:
                console.print(f"  [yellow]No signals[/yellow]")
                continue

            show_top(df_res, strat_name, session_label)

            best = df_res.iloc[0]
            result_row = {
                "strategy": strat_name,
                "session": session_label,
            }
            for k in sweep.keys():
                result_row[k] = best.get(k, "-")
            for k in ("profit_factor", "win_rate", "total_pnl", "total_trades", "max_dd"):
                result_row[k] = best.get(k, 0)
            all_results.append(result_row)

    # ── Summary ──
    console.print(f"\n{'='*60}")
    console.print("[bold]📋 日盤 vs 夜盤 最佳參數比較[/bold]")
    console.print(f"{'='*60}\n")

    summary = pd.DataFrame(all_results)
    if summary.empty:
        console.print("[yellow]No results![/yellow]")
        return

    EXPORTS.mkdir(parents=True, exist_ok=True)
    out_path = EXPORTS / "day_night_sweep.csv"
    summary.to_csv(out_path, index=False)
    console.print(f"[green]📁 Saved: {out_path}[/green]\n")

    table = Table(title="比較報表")
    table.add_column("策略", style="bold")
    table.add_column("時段")
    table.add_column("PF", justify="right")
    table.add_column("WR%", justify="right")
    table.add_column("PnL", justify="right")
    table.add_column("Trades", justify="right")
    table.add_column("MaxDD%", justify="right")
    table.add_column("最佳參數")

    for _, row in summary.iterrows():
        pf = row.get("profit_factor", 0)
        pf_color = "green" if pf >= 1.5 else ("yellow" if pf >= 1.0 else "red")
        pnl = row.get("total_pnl", 0)
        pnl_color = "green" if pnl > 0 else "red"

        params = []
        for k in SWEEP_PARAMS.get(row["strategy"], {}).keys():
            v = row.get(k, "-")
            params.append(f"{k}={v}")

        table.add_row(
            row["strategy"], row["session"],
            f"[{pf_color}]{pf:.2f}[/{pf_color}]",
            f"{row.get('win_rate', 0):.1f}",
            f"[{pnl_color}]{pnl:+,.0f}[/{pnl_color}]",
            str(int(row.get("total_trades", 0))),
            f"{row.get('max_dd', 0):.1f}",
            " ".join(params),
        )
    console.print(table)

    # ── Recommendations ──
    console.print(f"\n{'='*60}")
    console.print("[bold]💡 建議[/bold]")
    console.print(f"{'='*60}\n")

    for strat_name in strategies:
        mask = summary["strategy"] == strat_name
        day = summary[mask & (summary["session"] == "日盤 ☀️")]
        night = summary[mask & (summary["session"] == "夜盤 🌙")]

        if day.empty or night.empty:
            continue

        d, n = day.iloc[0], night.iloc[0]
        console.print(f"\n**{strat_name}**:")
        console.print(f"  日盤最佳: PF={d['profit_factor']:.2f}, WR={d['win_rate']:.1f}%, "
                      f"PnL={d['total_pnl']:+,.0f}, trades={int(d['total_trades'])}")
        console.print(f"  夜盤最佳: PF={n['profit_factor']:.2f}, WR={n['win_rate']:.1f}%, "
                      f"PnL={n['total_pnl']:+,.0f}, trades={int(n['total_trades'])}")

        if n["profit_factor"] < 1.0:
            console.print(f"  ⚠️ 夜盤 PF<1.0，建議日盤交易")
        elif n["profit_factor"] >= 1.3 and d["profit_factor"] >= 1.3:
            console.print(f"  ✅ 日夜盤皆可，建議使用各自最佳參數")
        elif n["profit_factor"] > d["profit_factor"]:
            console.print(f"  🌙 夜盤表現優於日盤")


if __name__ == "__main__":
    main()
