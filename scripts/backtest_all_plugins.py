#!/usr/bin/env python3
"""
Bar-by-bar backtest for plugin strategies with full Monitor exit management.

Uses core.exit_manager.ExitManager for TP1, trailing, VWAP, and stop loss —
identical to live trading logic in FuturesMonitor.
"""
import sys
sys.path.insert(0, '.')
import pandas as pd
import numpy as np
from pathlib import Path

from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.backtest_report import TradeRecord, calculate_backtest_metrics, generate_detailed_report
from core.exit_manager import ExitManager, ExitConfig


def load_data():
    """Load and resample TMF data to 5m bars with indicators."""
    csv = Path("data/tmf_full_2026.csv")
    if not csv.exists():
        print(f"❌ {csv} not found")
        return None
    df = pd.read_csv(csv, parse_dates=["timestamp"], index_col="timestamp")
    df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
    df = df.resample("5min").agg({
        "Open": "first", "High": "max", "Low": "min",
        "Close": "last", "Volume": "sum"
    }).dropna()
    from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
    df = calculate_futures_squeeze(df)
    df = df.dropna(subset=["Close"])
    print(f"📊 Loaded {len(df)} 5m bars ({df.index[0].date()} → {df.index[-1].date()})")
    return df


def backtest_strategy(strategy, df: pd.DataFrame, exit_mgr: ExitManager) -> list[TradeRecord]:
    """Run strategy with full Monitor exit management (TP1, trailing, VWAP, SL)."""
    strategy.init(StrategyContext(
        market=MarketData(last_bar={}),
        position=PositionView(),
        config={"params": {}},
        bar_counter=0
    ))

    point_value = 200.0
    fee = 20.0 * 2
    tax_rate = 2e-5

    trades: list[TradeRecord] = []
    trade_id = 0
    position = 0        # 0=flat, +1=long, -1=short
    entry_price = 0.0
    entry_bar = 0
    sig_entry = None    # Store the entry signal

    for i in range(50, len(df)):
        bar = df.iloc[i]
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
            "squeeze_on": bar.get("sqz_on", False),
            "ema_fast": bar.get("ema_fast", 0),
            "ema_slow": bar.get("ema_slow", 0),
        }

        price = bar["Close"]
        vwap = bar.get("vwap", price)

        ctx = StrategyContext(
            market=MarketData(
                last_bar=last_bar_dict,
                df_5m=df.iloc[max(0, i - 100):i + 1]
            ),
            position=PositionView(
                size=position,
                entry_price=entry_price,
                current_stop_loss=exit_mgr.state.current_sl if position != 0 else None,
            ),
            config={"params": {}},
            bar_counter=i
        )

        sig = strategy.on_bar(ctx)

        # ── Handle EXIT logic (via ExitManager) ──────────────────────
        if position != 0:
            exit_mgr.state.position = position
            exit_result = exit_mgr.on_bar(price, vwap, i)

            if exit_result:
                # Calculate PnL
                pnl_pts = (price - entry_price) * position
                gross = pnl_pts * point_value
                tax = (entry_price + price) * point_value * tax_rate
                net = gross - fee * exit_result["lots"] - tax
                risk = abs(entry_price - exit_mgr.state.initial_sl) * point_value
                risk_r = risk if risk > 0 else 1.0

                trade_id += 1
                trades.append(TradeRecord(
                    trade_id=trade_id,
                    strategy_name=strategy.name,
                    action=exit_result["action"],
                    entry_price=entry_price,
                    exit_price=exit_result["price"],
                    pnl_points=pnl_pts,
                    pnl_dollars=net,
                    risk_r=risk_r,
                    bars_held=exit_result["bars_held"],
                    reason=exit_result["reason"],
                    entry_time=str(df.index[entry_bar]),
                    exit_time=str(df.index[i]),
                ))

                if exit_result["action"] == "EXIT":
                    position = 0
                    entry_price = 0.0
                elif exit_result["action"] == "PARTIAL_EXIT":
                    # For single-lot: TP1 = full exit
                    position = 0
                    entry_price = 0.0
                continue  # Don't enter same bar as exit

        # ── Handle ENTRY logic ───────────────────────────────────────
        if sig and position == 0 and sig.action in ("BUY", "SELL"):
            position = 1 if sig.action == "BUY" else -1
            entry_price = price
            entry_bar = i
            sig_entry = sig
            exit_mgr.on_entry(price, sig.stop_loss, i)

    return trades


if __name__ == "__main__":
    df = load_data()
    if df is None:
        sys.exit(1)

    # Monitor exit config (matches futures.yaml)
    exit_cfg = ExitConfig(
        tp1_pts=50.0,
        tp1_lots=1,
        trailing_trigger_pts=100.0,
        trailing_distance_pts=50.0,
        exit_on_vwap=True,
        vwap_confirm_bars=2,
        lots_per_trade=1,
        max_positions=1,
    )
    exit_mgr = ExitManager(exit_cfg)

    reg = StrategyRegistry()
    reg.discover()

    print("\n🔍 Plugin Backtest (Monitor exit management: TP1 + Trailing + VWAP + SL)")
    print("=" * 95)
    print(f"  {'Strategy':<25s} {'PnL':>10s} {'WR':>6s} {'DD':>7s} {'PF':>6s} {'Exp$':>7s} {'R':>6s} {'Trades':>7s}")
    print("-" * 95)

    all_trades = []
    for item in reg.list_all():
        if item.get("asset_class") != "futures":
            continue
        name = item["name"]
        strategy = reg.get(name)
        if strategy is None:
            continue

        # Reset exit manager for each strategy
        exit_mgr = ExitManager(exit_cfg)
        trades = backtest_strategy(strategy, df, exit_mgr)
        all_trades.extend(trades)

        metrics = calculate_backtest_metrics(trades)
        if "error" in metrics:
            print(f"  {name:<25s} {'—':>10s} {'—':>6s} {'—':>7s} {'—':>6s} {'—':>7s} {'—':>6s} {0:>7}")
            continue

        print(f"  {name:<25s} {metrics['total_pnl']:>10,.0f} {metrics['win_rate']:5.1f}% "
              f"{metrics['max_dd_pct']:6.1f}% {metrics['profit_factor']:>6} "
              f"{metrics['expectancy_usd']:>7,.0f} {metrics['expectancy_r']:>6.2f} "
              f"{metrics['total_trades']:>7}")

    print("=" * 95)

    # Print detailed report for top strategy
    print("\n📋 Detailed Report — Counter-VWAP")
    cvw_trades = [t for t in all_trades if t.strategy_name == "counter_vwap"]
    if cvw_trades:
        metrics = calculate_backtest_metrics(cvw_trades)
        report = generate_detailed_report(metrics, "Counter-VWAP", expected_pf=1.95, expected_wr=40.7)
        print(report)
