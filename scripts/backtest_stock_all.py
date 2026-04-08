"""
Phase 2: Backtest all 7 stock strategies on 3-month data.
Usage: python3 scripts/backtest_stock_all.py
"""
import sys
import os
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import pandas_ta  # Must import to register 'df.ta' accessor
import yaml

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
from backtest.stock_engine import simulate_stock_trades, calculate_stock_metrics
from strategies.stocks.entry_strategies import STOCK_STRATEGIES

DATA_DIR = ROOT / "data" / "taifex_raw"
CONFIG_PATH = ROOT / "config" / "stocks.yaml"


def strategy_to_signals(strategy_name, df, cfg):
    """Convert a stock strategy function to boolean long/short signal arrays."""
    strat_fn = STOCK_STRATEGIES[strategy_name]["func"]
    n = len(df)
    long_signals = np.zeros(n, dtype=np.bool_)
    short_signals = np.zeros(n, dtype=np.bool_)

    for i in range(20, n):
        state = {
            "last_5m": df.iloc[i],
            "df_5m": df.iloc[:i + 1],
            "scout_stage": "IDLE",
            "scout_entry_price": 0.0,
            "market_trend": "BULL",
            "is_bear_market": False,
        }
        # Reset state for each bar (simulate fresh check each bar)
        res = strat_fn(state, cfg)
        if res and res["action"] == "BUY":
            long_signals[i] = True
        # No short strategies for stocks yet
        short_signals[i] = False

    return long_signals, short_signals


def backtest_ticker(ticker, strategy_name, cfg):
    """Backtest one strategy on one ticker."""
    file_path = DATA_DIR / f"STOCK_{ticker}_5m.csv"
    if not file_path.exists():
        return None

    df = pd.read_csv(file_path)
    if df.empty or len(df) < 50:
        return None

    # Standardize columns
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if cl == "ts":
            col_map[c] = "timestamp"
        elif cl in ("open", "high", "low", "close", "volume"):
            col_map[c] = c.capitalize()
    df = df.rename(columns=col_map)

    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        df = df.set_index("timestamp").sort_index()
    elif "Date" in df.columns:
        df["Date"] = pd.to_datetime(df["Date"])
        df = df.set_index("Date").sort_index()
    elif "ts" in df.columns:
        df["ts"] = pd.to_datetime(df["ts"])
        df = df.set_index("ts").sort_index()

    # Calculate indicators
    df = calculate_stock_squeeze(df)

    # Fill NaN
    for col in ["macd_hist", "k_val", "d_val", "adx", "bb_lower", "bb_mid", "bb_upper"]:
        if col in df.columns:
            df[col] = df[col].fillna(0)
    if "macd_rising" in df.columns:
        df["macd_rising"] = df["macd_rising"].fillna(False)

    stk_cfg = cfg.get("stocks", {})
    capital_per_trade = stk_cfg.get("capital_per_trade", 20000)
    stop_loss_pct = stk_cfg.get("stop_loss_pct", 0.02)
    take_profit_pct = stk_cfg.get("take_profit_pct", 0.1)
    trailing_stop_pct = stk_cfg.get("trailing_stop_pct", 0.01)
    initial_balance = stk_cfg.get("total_portfolio_budget", 100000)

    # Trading day encoding
    df["day"] = pd.to_datetime(df.index).date
    day_codes = {}
    day_counter = 0
    trading_day_arr = np.zeros(len(df), dtype=np.int64)
    for i, d in enumerate(df["day"]):
        if d not in day_codes:
            day_codes[d] = day_counter
            day_counter += 1
        trading_day_arr[i] = day_codes[d]

    # Generate signals
    try:
        long_signals, short_signals = strategy_to_signals(strategy_name, df, cfg)
    except Exception as e:
        return {"ticker": ticker, "strategy": strategy_name, "error": str(e)}

    # Run backtest
    entries, exits, positions, pnl_arr, reasons, quantities = simulate_stock_trades(
        df["Close"].values,
        df["High"].values,
        df["Low"].values,
        trading_day_arr,
        long_signals,
        short_signals,
        initial_balance,
        capital_per_trade,
        stop_loss_pct,
        take_profit_pct,
        trailing_stop_pct,
    )

    metrics = calculate_stock_metrics(pnl_arr, initial_balance)

    # Max drawdown
    cumulative = np.cumsum(pnl_arr)
    running_max = np.maximum.accumulate(np.concatenate([[0.0], cumulative]))
    drawdown = cumulative - running_max[1:]
    max_dd = float(np.min(drawdown)) if len(drawdown) > 0 else 0.0

    # Profit factor: gross profit / gross loss from individual trades
    trades = pnl_arr[pnl_arr != 0]
    gross_profit = float(np.sum(trades[trades > 0])) if np.any(trades > 0) else 0.0
    gross_loss = abs(float(np.sum(trades[trades < 0]))) if np.any(trades < 0) else 1.0
    pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

    return {
        "ticker": ticker,
        "strategy": strategy_name,
        "total_pnl": round(metrics["total_pnl"], 0),
        "win_rate": round(metrics["win_rate"], 1),
        "total_trades": int(metrics["total_trades"]),
        "max_drawdown": round(max_dd, 0),
        "profit_factor": round(pf, 2),
        "data_days": len(df),
        "data_range": f"{df.index[0].date()} ~ {df.index[-1].date()}" if len(df) > 0 else "",
    }


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH))
    stk_cfg = cfg.get("stocks", {})
    watchlist = stk_cfg.get("watchlist", [])

    strategies = list(STOCK_STRATEGIES.keys())
    print(f"Backtesting {len(strategies)} strategies × {len(watchlist)} tickers = {len(strategies) * len(watchlist)} combos")
    print(f"Period: 3 months (5m bars)")
    print(f"SL={stk_cfg.get('stop_loss_pct', 0.02)*100}% TP={stk_cfg.get('take_profit_pct', 0.1)*100}% TS={stk_cfg.get('trailing_stop_pct', 0.015)*100}%")
    print()

    results = []
    for s_idx, strat in enumerate(strategies, 1):
        for t_idx, ticker in enumerate(watchlist, 1):
            print(f"  [{s_idx}/{len(strategies)}] [{t_idx}/{len(watchlist)}] {ticker} {strat}...", end=" ")
            res = backtest_ticker(ticker, strat, cfg)
            if res:
                if "error" in res:
                    print(f"ERROR: {res['error']}")
                else:
                    print(f"PnL={res['total_pnl']:+.0f} PF={res['profit_factor']:.2f} T={res['total_trades']}")
                results.append(res)
            else:
                print("NO DATA")

    # Summary table
    df_results = pd.DataFrame(results)
    print("\n" + "=" * 80)
    print("STRATEGY SUMMARY (sorted by total PnL)")
    print("=" * 80)

    strat_summary = df_results.groupby("strategy").agg({
        "total_pnl": "sum",
        "win_rate": "mean",
        "total_trades": "sum",
        "max_drawdown": "mean",
        "profit_factor": "mean",
    }).sort_values("total_pnl", ascending=False)

    print(strat_summary.to_string())

    # Top 10 individual combos
    print("\n" + "=" * 80)
    print("TOP 10 COMBINATIONS (by PnL)")
    print("=" * 80)
    top10 = df_results.sort_values("total_pnl", ascending=False).head(10)
    print(top10[["ticker", "strategy", "total_pnl", "win_rate", "total_trades", "profit_factor", "max_drawdown"]].to_string(index=False))

    # Save
    out_path = ROOT / "exports" / "stock_backtest_all.csv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_results.to_csv(out_path, index=False)
    print(f"\n📊 Results saved to {out_path}")


if __name__ == "__main__":
    main()
