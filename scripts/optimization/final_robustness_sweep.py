"""
Final Robustness Sweep — 3-Year multi-strategy optimization.
Systematically finds the best parameters across 820k bars of TMF data.
"""
import sys
import pandas as pd
from pathlib import Path
import logging

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.parameter_optimizer import GridSearchOptimizer
from core.backtest_engine import AssetProfile, AssetType
from core.data_manager import data_manager
from core.strategy_registry import StrategyRegistry

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def run_final_sweep():
    # 1. Load 3-Year Data
    print("📊 Loading 3-year Parquet Database...")
    df = data_manager.load_historical("TXFR1")
    if df.empty:
        print("❌ Data not found. Run expansion first.")
        return

    profile = AssetProfile(asset_type=AssetType.FUTURES, point_value=200, margin_per_lot=170000)
    optimizer = GridSearchOptimizer(profile=profile)
    reg = StrategyRegistry()
    reg.discover()

    # 2. Define Grids for core strategies
    # We focus on the most promising ones to ensure completion
    sweep_plan = [
        {
            "name": "orb_breakout",
            "grid": {
                "range_bars": [6, 12, 18], # 30min, 60min, 90min
                "atr_mult": [1.5, 2.5, 3.5]
            }
        },
        {
            "name": "vol_squeeze",
            "grid": {
                "entry_score": [5, 15, 25],
                "atr_mult": [1.0, 2.0]
            }
        },
        {
            "name": "spring_upthrust",
            "grid": {
                "atr_mult": [1.5, 2.0, 3.0]
            }
        }
    ]

    all_summaries = []

    # 3. Execute Sweep
    for task in sweep_plan:
        name = task["name"]
        print(f"\n🔥 SWEEPING: {name} ({len(df)} bars)")
        try:
            results = optimizer.run_sweep(name, df, task["grid"], max_workers=4)
            if not results.empty and "cagr" in results.columns:
                results["strategy"] = name
                all_summaries.append(results)
                
                top = results.sort_values("sharpe", ascending=False).head(3)
                print(f"✅ {name} Top Result: Sharpe={top['sharpe'].max():.2f}")
            else:
                print(f"⚠️ {name} generated no trades in 3 years.")
        except Exception as e:
            print(f"❌ {name} sweep failed: {e}")

    # 4. Final Leaderboard
    if all_summaries:
        final_df = pd.concat(all_summaries)
        leaderboard = final_df.sort_values("sharpe", ascending=False).head(15)
        
        out_path = Path("exports/optimization/final_robustness_leaderboard.csv")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        final_df.to_csv(out_path, index=False)
        
        print("\n" + "=" * 80)
        print("🏆 3-YEAR ROBUSTNESS LEADERBOARD (Top 15)")
        print("=" * 80)
        # Identify columns to show (Strategy + params + metrics)
        cols = ["strategy", "cagr", "sharpe", "max_dd", "trade_count"]
        print(leaderboard[cols].to_string(index=False))
        print(f"\n📁 Full sweep results saved to {out_path}")
    else:
        print("\nNo strategies produced viable results.")

if __name__ == "__main__":
    run_final_sweep()
