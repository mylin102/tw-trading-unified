#!/usr/bin/env python3
"""
Unified Backtest Runner V2 — 公平比較所有策略

Features:
- 逐 bar 模擬真實進出場 (使用 core.backtest_engine)
- 統一手續費/稅率計算
- 自動發現 Registry 所有策略
- 比較 CANSLIM, Futures Plugins, Stock Strategies
- 輸出 CSV + Markdown 報告

Usage:
    python3 backtest/unified_runner.py
"""
import sys
sys.path.insert(0, '.')

import os
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional
import yaml

# Internal imports
from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.signal import Signal
from core.backtest_engine import BacktestEngine, AssetProfile, AssetType

# ==================== Configuration ====================
START_DATE = '2020-01-01'
END_DATE = '2026-04-01'
INITIAL_CAPITAL = 1_000_000

# Asset Profiles
FUTURES_PROFILE = AssetProfile(
    asset_type=AssetType.FUTURES,
    point_value=200,
    margin_per_lot=170000,
    fee_rate=0.00002,
    tax_rate=0.00002
)

STOCK_PROFILE = AssetProfile(
    asset_type=AssetType.STOCK,
    point_value=1,
    margin_per_lot=0,
    fee_rate=0.001425,
    tax_rate=0.003
)

# ==================== Backtest Logic ====================
def execute_strategy_backtest(df, strategy, profile: AssetProfile):
    """Bridge function to use the new BacktestEngine"""
    engine = BacktestEngine(profile=profile, initial_capital=INITIAL_CAPITAL)
    result = engine.run(df, strategy)
    return result

# ==================== Unified Runner ====================
def run_all_strategies():
    """Run all strategies and generate comparison report."""
    print("=" * 70)
    print("🚀 統一回測引擎 V2 — 公平比較所有策略")
    print("=" * 70)
    print(f"期間: {START_DATE} → {END_DATE}")
    print(f"初始資金: ${INITIAL_CAPITAL:,.0f}")
    print()

    results = []
    reg = StrategyRegistry()
    reg.discover()

    # 1. Load TMF historical data (used for all futures strategies)
    print("📊 [1/3] Loading TMF historical data (Parquet)...")
    from core.data_manager import data_manager
    df_tmf = data_manager.load_historical("TXFR1")
    
    if df_tmf.empty:
        print("  ❌ No historical TMF data found in database.")
        df_tmf = None
    else:
        print(f"  ✅ Loaded {len(df_tmf)} 5m bars ({df_tmf.index[0].date()} → {df_tmf.index[-1].date()})")

    # 2. Run Futures Plugin Strategies
    if df_tmf is not None:
        print("\n📊 [2/3] Running Futures Plugins...")
        for item in reg.list_all():
            if item.get("asset_class") != "futures" or not item.get("available"):
                continue
            name = item["name"]
            strategy = reg.get(name)
            if strategy is None:
                continue

            try:
                res = execute_strategy_backtest(df_tmf, strategy, FUTURES_PROFILE)
                metrics = res.metrics
                
                if metrics:
                    results.append({
                        'strategy': name,
                        'asset_class': 'futures',
                        'cagr': metrics.get('cagr', 0),
                        'total_return': metrics.get('total_pnl', 0) / INITIAL_CAPITAL,
                        'sharpe': metrics.get('sharpe', 0),
                        'max_dd': metrics.get('mdd', 0) / INITIAL_CAPITAL,
                        'win_rate': metrics.get('win_rate', 0),
                        'profit_factor': metrics.get('profit_factor', 0),
                        'trades': metrics.get('trade_count', 0),
                    })
                    print(f"  ✅ {name}: CAGR={metrics.get('cagr', 0):.2%}  Trades={metrics.get('trade_count', 0)}")
                else:
                    print(f"  ⚠️ {name}: No results")
            except Exception as e:
                print(f"  ❌ {name} error: {e}")

    # 3. Run CANSLIM
    print("\n📊 [3/3] Running CANSLIM (Technical)...")
    try:
        from scripts.backtest.backtest_canslim import Backtester as CANSLIMBacktester
        import yfinance as yf
        
        UNIVERSE = ['2330', '2317', '2454'] # Reduced universe for speed
        price_data = {}
        for t in UNIVERSE:
            try:
                df = yf.Ticker(f"{t}.TW").history(start=START_DATE, end=END_DATE, auto_adjust=False)
                if len(df) > 60:
                    price_data[t] = df[['Open', 'High', 'Low', 'Close', 'Volume']]
            except: pass
        
        if price_data:
            bt = CANSLIMBacktester(INITIAL_CAPITAL)
            m = bt.run(price_data)
            if m:
                results.append({
                    'strategy': 'CANSLIM (Technical)',
                    'asset_class': 'stocks',
                    'cagr': m.get('CAGR', 0),
                    'total_return': m.get('Total Return', 0),
                    'sharpe': m.get('Sharpe', 0),
                    'max_dd': m.get('Max Drawdown', 0),
                    'win_rate': m.get('Win Rate', 0),
                    'profit_factor': m.get('Profit Factor', 0),
                    'trades': m.get('Trades', 0),
                })
                print(f"  ✅ CANSLIM: CAGR={m.get('CAGR', 0):.2%}  PF={m.get('Profit Factor', 0):.2f}")
    except Exception as e:
        print(f"  ❌ CANSLIM error: {e}")

    # 4. Generate Report
    if not results:
        print("\n⚠️ No results to report")
        return None
    
    print("\n📊 生成報告...")
    df_results = pd.DataFrame(results)
    df_results = df_results.sort_values('cagr', ascending=False)
    
    report_dir = Path("exports/backtest")
    report_dir.mkdir(parents=True, exist_ok=True)
    csv_path = report_dir / "unified_report.csv"
    df_results.to_csv(csv_path, index=False)
    
    md_path = report_dir / "unified_report.md"
    with open(md_path, 'w') as f:
        f.write("# 📊 Unified Backtest Report\n\n")
        f.write(f"**期間**: {START_DATE} → {END_DATE}  \n")
        f.write(f"**初始資金**: ${INITIAL_CAPITAL:,.0f}  \n")
        f.write(f"**生成時間**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")

        f.write("## 策略比較\n\n")
        f.write("| 策略 | 資產類別 | CAGR | 總報酬 | Sharpe | MaxDD | 勝率 | PF | 交易數 |\n")
        f.write("|------|---------|------|--------|--------|-------|------|----|--------|\n")
        for _, row in df_results.iterrows():
            f.write(f"| {row['strategy']} | {row['asset_class']} | "
                    f"{row['cagr']:.2%} | {row['total_return']:.2%} | "
                    f"{row['sharpe']:.2f} | {row['max_dd']:.2%} | "
                    f"{row['win_rate']:.1%} | {row['profit_factor']:.2f} | "
                    f"{row['trades']:.0f} |\n")

    print(f"  ✅ 報告已儲存: {csv_path}")
    print(f"  ✅ Markdown: {md_path}")
    print("\n" + "=" * 70)
    print(df_results[['strategy', 'cagr', 'win_rate', 'trades']].to_string(index=False))
    return df_results

if __name__ == "__main__":
    run_all_strategies()
