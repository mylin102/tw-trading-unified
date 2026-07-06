"""
Annualized Robustness Sweep — Segmented optimization to find cross-year winners.
Avoids timeouts by splitting the 3-year dataset into yearly chunks.
"""
import sys
import pandas as pd
from pathlib import Path

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.parameter_optimizer import GridSearchOptimizer
from core.backtest_engine import AssetProfile, AssetType
from core.data_manager import data_manager

def run_annual_sweep():
    print("📊 Loading 3-year Parquet Database...")
    df_full = data_manager.load_historical("TXFR1")
    if df_full.empty: return

    # Split into years
    years = [2023, 2024, 2025]
    
    profile = AssetProfile(asset_type=AssetType.FUTURES, point_value=200, margin_per_lot=170000)
    optimizer = GridSearchOptimizer(profile=profile)

    # Strategy: vol_squeeze (our most promising complex strategy)
    name = "vol_squeeze"
    grid = {"entry_score": [10, 25], "atr_mult": [1.5, 2.5]} # 4 combinations
    
    annual_results = []

    for year in years:
        print(f"\n📅 --- Analyzing Year: {year} ---")
        df_year = df_full[df_full.index.year == year]
        if df_year.empty: continue
        
        res = optimizer.run_sweep(name, df_year, grid, max_workers=4)
        if not res.empty:
            res["year"] = year
            annual_results.append(res)

    if annual_results:
        final_df = pd.concat(annual_results)
        # Find parameters that are profitable in ALL years
        summary = final_df.groupby(["entry_score", "atr_mult"]).agg({
            "cagr": ["mean", "min", "max"],
            "win_rate": "mean",
            "trade_count": "sum"
        })
        print("\n" + "=" * 80)
        print(f"🏆 {name.upper()} CROSS-YEAR CONSISTENCY REPORT")
        print("=" * 80)
        print(summary)
    else:
        print("No trades generated in any year.")

if __name__ == "__main__":
    run_annual_sweep()
