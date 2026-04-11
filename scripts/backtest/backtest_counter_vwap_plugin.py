#!/usr/bin/env python3
"""
Backtest Counter-VWAP plugin directly using the StrategyBase interface.
This faithfully reproduces the logic from the plugin (not the vectorized approximation).
"""
import sys
sys.path.insert(0, '.')
import os
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
from itertools import product

from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData


def load_data():
    csv = Path("data/tmf_full_2026.csv")
    if not csv.exists():
        print(f"❌ {csv} not found")
        return None
    df = pd.read_csv(csv, parse_dates=["timestamp"], index_col="timestamp")
    # Keep only needed columns, resample to 5m
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df = df.resample("5min").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum"
    }).dropna()
    # Calculate indicators once
    from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
    df = calculate_futures_squeeze(df)
    df = df.dropna(subset=["Close"])
    print(f"📊 Loaded {len(df)} 5m bars ({df.index[0].date()} → {df.index[-1].date()})")
    return df


def backtest(df: pd.DataFrame, confirm_bars: int, atr_sl_mult: float) -> dict:
    """Run Counter-VWAP plugin bar-by-bar with paper trading."""
    from strategies.plugins.futures.counter_vwap import CounterVWAP

    strat = CounterVWAP()
    strat.init(StrategyContext(
        market=MarketData(last_bar={}),
        position=PositionView(),
        config={"params": {"confirm_bars": confirm_bars, "atr_sl_mult": atr_sl_mult}},
        bar_counter=0
    ))

    balance = 100_000.0
    point_value = 200.0
    fee = 20.0 * 2  # round-trip per contract
    tax_rate = 2e-5

    position = 0  # 0=flat, 1=long, -1=short
    entry_price = 0.0
    stop_loss = 0.0
    trades = 0
    wins = 0
    total_pnl = 0.0
    peak_balance = balance
    max_dd = 0.0
    equity_curve = []

    for i in range(20, len(df)):
        bar = df.iloc[i]
        # Build last_bar dict with ALL fields the plugin needs
        last_bar_dict = {
            "Close": bar.get("Close", 0),
            "High": bar.get("High", 0),
            "Low": bar.get("Low", 0),
            "Volume": bar.get("Volume", 0),
            "fired": bar.get("fired", False),
            "momentum": bar.get("momentum", 0.0),
            "vwap": bar.get("vwap", bar.get("Close", 0)),
            "atr": bar.get("atr", 200.0),
            "mom_velo": bar.get("mom_velo", 0.0),
            "recent_high": bar.get("recent_high", bar.get("Close", 0)),
            "recent_low": bar.get("recent_low", bar.get("Close", 0)),
        }

        ctx = StrategyContext(
            market=MarketData(last_bar=last_bar_dict, df_5m=df.iloc[max(0,i-100):i+1]),
            position=PositionView(
                size=position,
                entry_price=entry_price,
                current_stop_loss=stop_loss if position != 0 else None,
            ),
            config={"params": {"confirm_bars": confirm_bars, "atr_sl_mult": atr_sl_mult}},
            bar_counter=i
        )

        sig = strat.on_bar(ctx)

        price = bar["Close"]
        if sig and position == 0:
            # Enter
            position = 1 if sig.action == "BUY" else -1
            entry_price = price
            stop_loss = sig.stop_loss
        elif position != 0:
            # Check exit
            exit_triggered = False
            if position == 1 and price <= stop_loss:
                exit_triggered = True
            elif position == -1 and price >= stop_loss:
                exit_triggered = True
            # Also check if plugin signals opposite direction (VWAP exit)
            if sig and ((position == 1 and sig.action == "SELL") or
                        (position == -1 and sig.action == "BUY")):
                exit_triggered = True

            if exit_triggered:
                # Calculate PnL
                pnl_pts = (price - entry_price) * position
                gross = pnl_pts * point_value
                tax = (entry_price + price) * point_value * tax_rate
                net = gross - fee - tax
                balance += net
                total_pnl += net
                trades += 1
                if net > 0:
                    wins += 1
                if balance > peak_balance:
                    peak_balance = balance
                dd = (peak_balance - balance) / peak_balance * 100
                if dd > max_dd:
                    max_dd = dd
                equity_curve.append(balance)
                position = 0
                entry_price = 0.0
                stop_loss = 0.0

    wr = wins / trades * 100 if trades > 0 else 0
    pf = (total_pnl + fee * trades + sum(
        [(bar["Close"] - entry_price) * position * point_value
         for bar, entry_price, position in []]
    ))  # Simplified: use gross PnL / max loss
    # Better PF: total gross profit / total gross loss
    # For simplicity, report raw metrics
    return {
        "confirm": confirm_bars,
        "atr_sl": atr_sl_mult,
        "pnl": round(total_pnl, 0),
        "trades": trades,
        "wr": round(wr, 1),
        "max_dd": round(max_dd, 1),
        "balance": round(balance, 0),
    }


if __name__ == "__main__":
    df = load_data()
    if df is None:
        sys.exit(1)

    print("\n🔍 Counter-VWAP Plugin Backtest (bar-by-bar, faithful)")
    print("=" * 70)

    combos = list(product([3, 5, 7], [1.5, 2.0, 3.0]))
    results = []
    for confirm, atr in combos:
        r = backtest(df, confirm, atr)
        results.append(r)
        pf_est = r["pnl"] / (abs(r["pnl"]) * 0.3 + 1) if r["trades"] > 0 else 0  # rough estimate
        print(f"  confirm={confirm} atr_sl={atr} → "
              f"PnL={r['pnl']:>8.0f} WR={r['wr']:5.1f}% DD={r['max_dd']:6.1f}% T={r['trades']}")

    results.sort(key=lambda x: x["pnl"], reverse=True)
    print(f"\n📊 Top 3 by PnL:")
    for r in results[:3]:
        print(f"  PF_est=?.?? WR={r['wr']:.1f}% confirm={r['confirm']} ATR={r['atr_sl']}x "
              f"PnL={r['pnl']:.0f} DD={r['max_dd']}% T={r['trades']}")
