"""
Strategy Evolution Comparison (Full Version) — GSD 4.8
Compares Baseline vs Shield vs Decision Intelligence across Multiple Strategies.
"""
import sys
from pathlib import Path
BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import pandas as pd
import numpy as np
import json
import datetime
from core.backtest_engine import BacktestEngine, AssetProfile, AssetType
from core.edge_model import edge_model
from core.strategy_registry import StrategyRegistry

def run_stage_backtest(strategy_name: str, stage_label: str, use_ml: bool, use_shield: bool, use_soft: bool):
    print(f"🚀 Running: {strategy_name} | {stage_label}...")
    
    from core import edge_model as edge_model_module
    
    # 1. Configure ML
    edge_model_module.edge_model._ml_model = None
    if use_ml:
        edge_model_module.edge_model._load_ml_model()
    
    original_evaluate = edge_model_module.edge_model.evaluate
    
    # 2. Inject Stage Logic
    def stage_wrapper(signal_score, context, strat_name):
        # Base Features
        features = {
            "trend_strength": 0.8 if context.get("regime") == "STRONG" else 0.4,
            "volatility": 0.5,
            "signal_strength": signal_score / 100.0,
            "vwap_distance": context.get("vwap_dist", 0) / 100.0,
            "trend_strength_raw": context.get("trend_strength_raw", 0.0),
            "breakout_strength": context.get("breakout_strength", 0.0),
            "volume_spike": context.get("volume_spike", 1.0)
        }
        
        prob = edge_model_module.edge_model.compute_edge(features) if use_ml else 0.51
        
        # Shield Logic
        if use_shield:
            regime = context.get("regime", "NORMAL")
            trend_raw = features["trend_strength_raw"]
            side = context.get("side", "UNKNOWN")
            if regime == "STRONG":
                if trend_raw > 0.002 and side == "SHORT": return {"has_edge": False, "rank": "SHIELD"}
                if trend_raw < -0.002 and side == "LONG": return {"has_edge": False, "rank": "SHIELD"}

        # Threshold logic
        threshold = 0.65 if (use_ml and not use_soft) else 0.50
        has_edge = prob >= threshold
        
        pos_scale = 1.0
        if use_soft and has_edge:
             base = 0.45 if "orb" in strat_name else 0.40
             pos_scale = max(0.1, (prob - base) * 2.5)
             
        return {
            "has_edge": has_edge,
            "edge_score": prob,
            "pos_scale": pos_scale if has_edge else 0.0,
            "rank": "ACTIVE",
            "is_exploring": False,
            "features": features
        }

    edge_model_module.edge_model.evaluate = stage_wrapper
    
    # 3. Load & Clean Data
    data_path = BASE / "data" / "historical" / "TXFR1_5m.parquet"
    df = pd.read_parquet(data_path).tail(10000)
    
    # [GSD Aggressive Cleaning]
    df = df[~df.index.duplicated(keep='last')]
    df = df[df.index.notnull()]
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    
    # Pre-enrich to avoid engine issues
    from core.data_enricher import enricher
    # Ensure indicator required by registry/strategy are pre-calculated
    df = enricher.enrich(df, ["atr", "vwap", "alpha"])
    
    # 4. Load Engine & Registry
    profile = AssetProfile(asset_type=AssetType.FUTURES, point_value=200.0, margin_per_lot=180000.0, fee_rate=0.00002)
    engine = BacktestEngine(profile)
    registry = StrategyRegistry()
    registry.discover()
    
    strat = registry.get(strategy_name)
    
    # Force 5 lots to see scaling clearly
    original_on_bar = strat.on_bar
    def on_bar_with_qty(ctx):
        sig = original_on_bar(ctx)
        if sig: sig.quantity = 5
        return sig
    strat.on_bar = on_bar_with_qty

    # RUN
    result = engine.run(df, strat)
    
    # Clean up
    edge_model_module.edge_model.evaluate = original_evaluate
    strat.on_bar = original_on_bar
    
    return {
        "Strategy": strategy_name,
        "Stage": stage_label,
        "CAGR": f"{result.metrics['cagr']*100:.2f}%",
        "WinRate": f"{result.metrics['win_rate']*100:.1f}%",
        "Trades": result.metrics["trade_count"],
        "PF": round(result.metrics["profit_factor"], 2)
    }

def main():
    strats = ["counter_vwap", "orb_breakout"]
    stages = [
        ("Baseline (0.5 Hard)", False, False, False),
        ("Shield Only", False, True, False),
        ("Full DI (Shield + Soft)", True, True, True)
    ]
    
    final_results = []
    for s_name in strats:
        for label, ml, shield, soft in stages:
            final_results.append(run_stage_backtest(s_name, label, ml, shield, soft))
        
    df_cmp = pd.DataFrame(final_results)
    print("\n" + "="*35 + " EVOLUTION COMPARISON " + "="*35)
    print(df_cmp.to_string(index=False))
    print("="*92)

if __name__ == "__main__":
    main()
