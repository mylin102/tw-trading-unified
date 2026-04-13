"""
Optimize KalmanMomentum — Dedicated sweep for Kalman Filter parameters.
Uses the verified GridSearchOptimizer to find the best sensitivity/stop-loss balance.
"""
import sys
from pathlib import Path
import pandas as pd

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.parameter_optimizer import GridSearchOptimizer
from core.backtest_engine import AssetProfile, AssetType
from core.data_manager import data_manager

def run_kalman_optimization():
    print("=" * 80)
    print("🚀 KALMAN MOMENTUM PARAMETER OPTIMIZATION")
    print("=" * 80)

    # 1. Load Data
    df = data_manager.load_historical("TXFR1")
    if df.empty:
        print("❌ Error: TMF Parquet data not found.")
        return
    print(f"✅ Loaded {len(df)} bars for optimization.")

    # 2. Setup Optimizer
    profile = AssetProfile(asset_type=AssetType.FUTURES, point_value=200, margin_per_lot=170000)
    optimizer = GridSearchOptimizer(profile=profile, initial_capital=1000000)

    # 3. Define Grid
    # sensitivity: controls how much 'velocity' is needed to enter
    # atr_mult: controls the stop-loss distance
    param_grid = {
        "sensitivity": [0.00001, 0.00005, 0.0001, 0.0002, 0.0005],
        "atr_mult": [1.5, 2.0, 2.5, 3.0, 3.5]
    }

    # 4. Run Sweep (25 combinations)
    results_df = optimizer.run_sweep("kalman_momentum", df, param_grid, max_workers=4)

    # 5. Report Results
    if not results_df.empty:
        print("\n" + "=" * 80)
        print("📊 TOP 5 KALMAN CONFIGURATIONS (By CAGR)")
        print("=" * 80)
        # Sort and show top
        top_5 = results_df.sort_values("cagr", ascending=False).head(5)
        print(top_5[["sensitivity", "atr_mult", "cagr", "sharpe", "win_rate", "trade_count"]].to_string(index=False))
        
        # Save results for dashboard
        out_path = Path("exports/optimization/kalman_sweep_results.csv")
        out_path.parent.mkdir(parents=True, exist_ok=True)
        results_df.to_csv(out_path, index=False)
        print(f"\n📁 Full results saved to {out_path}")
    else:
        print("⚠️ No results generated.")

if __name__ == "__main__":
    run_kalman_optimization()
