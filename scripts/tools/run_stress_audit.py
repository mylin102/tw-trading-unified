"""
Stress Audit Tool — Comprehensive comparison of strategy resilience.
Runs backtests followed by Monte Carlo stress testing for all plugins.
"""
import sys
from pathlib import Path
import pandas as pd
import numpy as np
from datetime import datetime

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.strategy_registry import StrategyRegistry
from core.backtest_engine import BacktestEngine, AssetProfile, AssetType
from core.backtest_storage import tracker
from core.monte_carlo import run_monte_carlo

def run_audit():
    print("=" * 80)
    print("🔬 STRATEGY STRESS AUDIT REPORT")
    print("=" * 80)
    
    # 1. Setup
    reg = StrategyRegistry()
    reg.discover()
    
    profile = AssetProfile(asset_type=AssetType.FUTURES, point_value=200, margin_per_lot=170000)
    engine = BacktestEngine(profile=profile)
    
    # Load Data via DataManager (Parquet)
    from core.data_manager import data_manager
    df_full = data_manager.load_historical("TXFR1")
    
    if df_full.empty:
        print("❌ Error: TMF Parquet data not found or empty.")
        return
    
    # Indicators are already pre-calculated in the backtest engine via DataEnricher, 
    # but for manual check we can ensure 'fired' exists
    if "fired" not in df_full.columns:
        from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
        df_full = calculate_futures_squeeze(df_full)
    
    audit_results = []
    
    # 2. Iterate Strategies
    for item in reg.list_all():
        if item.get("asset_class") != "futures": continue
        name = item["name"]
        print(f"\n▶️ Auditing {name}...")
        
        try:
            strat = reg.get(name)
            # Use loose test config to ensure trades are generated
            test_cfg = {"params": {"entry_score": 5, "atr_mult": 1.5}}
            
            # Run Backtest
            res = engine.run(df_full, strat, config=test_cfg)
            
            if res.metrics and res.metrics.get("trade_count", 0) > 1:
                # Save to database
                exp_id = tracker.save_experiment(res, params={}, tag="audit")
                
                # Run Monte Carlo
                mc = run_monte_carlo(res.trades, initial_capital=1000000)
                
                audit_results.append({
                    "Strategy": name,
                    "Trades": res.metrics["trade_count"],
                    "Hist. CAGR": f"{res.metrics['total_pnl']/1000000:.1%}",
                    "Hist. MDD": f"{res.metrics['mdd']:.1%}",
                    "95% VaR MDD": f"{mc['mdd_95']:.1%}",
                    "MDD Ratio": f"{mc['mdd_95'] / (res.metrics['mdd'] or 1):.1f}x",
                    "Prob. of Ruin": f"{mc['prob_of_ruin']:.1%}",
                    "Status": "🚩 HIGH RISK" if mc['prob_of_ruin'] > 0.05 or mc['mdd_95'] < -0.3 else "✅ STABLE"
                })
                print(f"  ✅ Audit Complete. PoR: {mc['prob_of_ruin']:.1%}")
            else:
                print(f"  ⚠️ Skipped: Not enough trades generated ({res.metrics.get('trade_count', 0)})")
        except Exception as e:
            print(f"  ❌ Failed: {e}")

    # 3. Print Comparison Table
    if audit_results:
        df_audit = pd.DataFrame(audit_results)
        print("\n" + "=" * 80)
        print("📊 COMPARISON: HISTORICAL VS. MONTE CARLO STRESS")
        print("=" * 80)
        print(df_audit.to_string(index=False))
        print("\n* MDD Ratio: How much worse the 95% worst-case is compared to history.")
        print("* Status: Based on 5% Ruin threshold and -30% VaR MDD.")
    else:
        print("\nNo strategies produced enough trades for a valid audit.")

if __name__ == "__main__":
    run_audit()
