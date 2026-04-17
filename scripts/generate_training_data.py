"""
Training Data Generator — Runs a backtest with 100% exploration to build a dataset.
This populates trade_attribution.csv with a wide variety of outcomes.
"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd
from core.backtest_engine import BacktestEngine, AssetProfile, AssetType
from core.edge_model import edge_model
from core.decision_logger import DecisionLogger
from core.strategy_registry import StrategyRegistry

def run_training_generation():
    print("🚀 Starting Training Data Generation (100% Exploration)...")
    
    registry = StrategyRegistry()
    registry.discover()
    
    # Force high exploration in the edge model
    # Note: we temporarily override the threshold or the random factor
    original_thresholds = edge_model.thresholds
    edge_model.thresholds = {k: -1.0 for k in original_thresholds} # Accept EVERYTHING
    edge_model.thresholds["default"] = -1.0
    
    # Load data
    data_path = BASE / "data" / "historical" / "TXFR1_5m.parquet"
    if not data_path.exists():
        print(f"❌ Historical data not found at {data_path}")
        return
        
    df = pd.read_parquet(data_path)
    
    # --- [GSD Aggressive Cleaning] ---
    # Fix duplicate indices and NaT which cause Pandas internal crashes
    df = df.sort_index()
    if df.index.duplicated().any():
        print(f"  ⚠️ Cleaning {df.index.duplicated().sum()} duplicate index entries...")
        df = df[~df.index.duplicated(keep='last')]
    df = df[df.index.notnull()]
    # Ensure a clean, unique datetime index
    df.index = pd.to_datetime(df.index)
    
    profile = AssetProfile(
        asset_type=AssetType.FUTURES,
        point_value=200.0, # Adjust for TX
        margin_per_lot=180000.0,
        fee_rate=0.00002
    )
    
    engine = BacktestEngine(profile)
    
    # Run key strategies
    for name in ["counter_vwap", "lr_momentum", "cumulative_delta"]:
        print(f"  📊 Generating data for {name}...")
        strat = registry.get(name)
        if not strat: continue
        
        engine.run(df.tail(10000), strat) # Use 10k bars for faster generation
        
    # Restore original model state
    edge_model.thresholds = original_thresholds
    print(f"✅ Data generation complete. Check logs/trade_attribution.csv")

if __name__ == "__main__":
    run_training_generation()
