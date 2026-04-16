#!/usr/bin/env python3
"""
Performance Comparison: Trailing Stop vs. Baseline
Follows V-cycle Validation for GSD Wave 5.4.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
from core.backtest_engine import BacktestEngine, AssetProfile, AssetType
from core.data_manager import data_manager
from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.signal import Signal

# Configuration
INITIAL_CAPITAL = 1_000_000
FUTURES_PROFILE = AssetProfile(
    asset_type=AssetType.FUTURES,
    point_value=200, # TMF
    margin_per_lot=170000,
    fee_rate=0.00002,
    tax_rate=0.00002
)

def run_comparison(strategy_name: str):
    print(f"\n🔍 Comparing Trailing Stop for: {strategy_name}")
    
    # 1. Load Data
    df = data_manager.load_historical("TXFR1")
    if df.empty:
        print("❌ Data not found")
        return
    
    # Use recent data for faster comparison
    df = df.iloc[-50000:] 
    
    reg = StrategyRegistry()
    reg.discover()
    strategy = reg.get(strategy_name)
    
    # 2. Run Baseline (Disable advanced exits)
    engine = BacktestEngine(FUTURES_PROFILE, INITIAL_CAPITAL)
    
    print("🏃 Running Baseline...")
    # Mock a config that disables triggers
    baseline_config = {"strategy": {"active_strategy": strategy_name}}
    
    # We need to manually override the signal output in a wrapper or temporary modification
    # but for simplicity, I'll just use the engine results if I can toggle them.
    # ACTUALLY, I'll modify the strategy instances.
    
    results = {}
    
    # Baseline: Set triggers to 0
    orig_on_bar = strategy.on_bar
    def baseline_on_bar(ctx):
        sig = orig_on_bar(ctx)
        if sig:
            sig.break_even_trigger = 0.0
            sig.trail_points = 0.0
        return sig
    strategy.on_bar = baseline_on_bar
    results['baseline'] = engine.run(df, strategy, baseline_config)
    
    # Adaptive: Use strategy defaults
    print("🏃 Running Adaptive (with Trailing Stop)...")
    strategy.on_bar = orig_on_bar
    results['adaptive'] = engine.run(df, strategy, baseline_config)
    
    # 3. Compare Results
    b = results['baseline'].metrics
    a = results['adaptive'].metrics
    
    print("\n" + "="*40)
    print(f"📊 RESULTS: {strategy_name}")
    print(f"{'Metric':<15} | {'Baseline':<12} | {'Adaptive':<12}")
    print("-"*40)
    print(f"{'Total PnL':<15} | {b['total_pnl']:>12,.0f} | {a['total_pnl']:>12,.0f}")
    print(f"{'Win Rate':<15} | {b['win_rate']:>12.1%} | {a['win_rate']:>12.1%}")
    print(f"{'Profit Factor':<15} | {b['profit_factor']:>12.2f} | {a['profit_factor']:>12.2f}")
    print(f"{'Trades':<15} | {b['trade_count']:>12.0f} | {a['trade_count']:>12.0f}")
    print("="*40)
    
    improvement = (a['total_pnl'] - b['total_pnl'])
    print(f"💰 PnL Delta: {improvement:+,.0f} TWD")

if __name__ == "__main__":
    for s in ["counter_vwap", "cumulative_delta"]:
        run_comparison(s)
