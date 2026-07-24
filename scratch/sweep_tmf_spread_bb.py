#!/usr/bin/env python3
# 2026-07-08 Gemini CLI: Sweep stop/trail parameters with BB filter enabled vs disabled.

import os
import sys
import glob
import pandas as pd
from typing import Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

# Add project root to path
sys.path.append('.')

# Enable backtest flag to disable state recovery and disk I/O in strategy
os.environ["MTS_BACKTEST"] = "1"

from scripts.backtest_spread_v2 import SpreadBacktester, DATA_PATTERN

def run_scenario(strat_name: str, dfs: List[pd.DataFrame], config: Dict[str, Any]) -> Dict[str, Any]:
    try:
        tester = SpreadBacktester(strat_name, config=config)
        # Force self._bb_enabled to matching config value
        tester.strategy._bb_enabled = config["params"]["release_filter"]["bb_enabled"]
        for df in dfs:
            tester.run_on_df(df)
        metrics = tester.get_metrics()
        if not metrics or metrics.get("trade_count", 0) == 0:
            return {}
        return metrics
    except Exception as e:
        print(f"Error running configuration: {e}", file=sys.stderr)
        return {}

def run_single_config(args) -> Dict[str, Any]:
    strat_name, dfs, config, label, stop_mult, trail_mult, bb_enabled = args
    metrics = run_scenario(strat_name, dfs, config)
    if metrics and metrics.get("trade_count", 0) > 0 and "total_net" in metrics:
        total_gross = metrics.get("total_gross", 0.0)
        total_costs = metrics.get("total_fees", 0.0) + metrics.get("total_taxes", 0.0)
        metrics["label"] = label
        metrics["stop_mult"] = stop_mult
        metrics["trail_mult"] = trail_mult
        metrics["bb_enabled"] = bb_enabled
        metrics["friction"] = total_costs / abs(total_gross if total_gross != 0 else 1)
        return metrics
    return {}

def main():
    files = sorted(glob.glob(DATA_PATTERN))
    if not files:
        print(f"No data files found matching {DATA_PATTERN}")
        return

    print(f"Loaded {len(files)} historical spread CSV files. Pre-processing and loading into memory...")
    
    bb_period = 20
    bb_std = 2.0
    dfs = []
    
    for f in files:
        df = pd.read_csv(f)
        if df.empty:
            continue
            
        # Calculate Bollinger Bands
        df["near_bb_mid"] = df["Close_near"].rolling(bb_period).mean()
        df["near_bb_std"] = df["Close_near"].rolling(bb_period).std()
        df["near_bb_upper"] = df["near_bb_mid"] + bb_std * df["near_bb_std"]
        df["near_bb_lower"] = df["near_bb_mid"] - bb_std * df["near_bb_std"]

        df["far_bb_mid"] = df["Close_far"].rolling(bb_period).mean()
        df["far_bb_std"] = df["Close_far"].rolling(bb_period).std()
        df["far_bb_upper"] = df["far_bb_mid"] + bb_std * df["far_bb_std"]
        df["far_bb_lower"] = df["far_bb_mid"] - bb_std * df["far_bb_std"]
        
        # Force sqz_on to True so that BB filter is active
        df["sqz_on"] = True
        
        ts_col = next((c for c in ["ts", "timestamp", "datetime"] if c in df.columns), None)
        if ts_col:
            df[ts_col] = pd.to_datetime(df[ts_col])
            df = df.set_index(ts_col)
        dfs.append(df)
        
    print(f"Successfully loaded {len(dfs)} DataFrames. Preparing sweep tasks...")

    tasks = []
    atr_stops = [1.0, 1.5, 2.0, 2.5]
    atr_trails = [0.1, 0.5, 1.0, 1.5, 2.0]
    bb_options = [False, True]

    for stop_mult in atr_stops:
        for trail_mult in atr_trails:
            for bb_enabled in bb_options:
                config = {
                    "params": {
                        "allow_night_session": True,
                        "regime": "WEAK",
                        "entry_z": 2.5,
                        "min_atr": 10.0,
                        "atr_multiplier_stop": stop_mult,
                        "atr_multiplier_trail": trail_mult,
                        "release_stop_points": 999.0,
                        "trail_distance_points": 999.0,
                        "release_filter": {
                            "bb_enabled": bb_enabled,
                            "bb_period": bb_period,
                            "bb_std_mult": bb_std,
                            "sell_within_bb_upper": 8.0,
                            "buy_within_bb_lower": 8.0,
                            "emergency_bypass_enabled": True,
                            "emergency_bypass_mult": 2.0
                        }
                    }
                }
                bb_label = "With BB" if bb_enabled else "No BB"
                label = f"ATR {stop_mult}x/{trail_mult}x ({bb_label})"
                tasks.append(("tmf_spread", dfs, config, label, stop_mult, trail_mult, bb_enabled))

    results = []
    print(f"Total configurations to evaluate: {len(tasks)}. Running in parallel (E-Cores)...")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(run_single_config, task): task for task in tasks}
        completed = 0
        for future in as_completed(futures):
            metrics = future.result()
            completed += 1
            if completed % 10 == 0 or completed == len(tasks):
                print(f"Progress: {completed}/{len(tasks)} configurations complete.")
            if metrics:
                results.append(metrics)

    results = sorted(results, key=lambda x: x.get("total_net", -999999), reverse=True)

    print("\nSweep Complete! Saving results to Markdown artifact...")
    
    artifact_dir = os.environ.get(
        "ARTIFACT_DIR", 
        "/Users/mylin/.gemini/antigravity-cli/brain/487d8f97-542a-4e2d-80e4-ddd146d2a064"
    )
    artifact_path = os.path.join(artifact_dir, "tmf_spread_bb_sweep_results.md")
    
    os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
    
    with open(artifact_path, "w") as f:
        f.write("# TMF Spread ATR + BB Optimization Sweep\n\n")
        f.write("Backtest evaluation period: All historical calendar spread days.\n")
        f.write("This sweep tests Stop Multipliers, Trailing Multipliers, and the impact of the Bollinger Band filter.\n\n")
        
        f.write("## Top 15 Configurations\n\n")
        f.write("| Rank | Configuration | Stop Mult | Trail Mult | BB Filter | Net PnL | Trades | Win% | Profit Factor | Avg Net | Friction |\n")
        f.write("|---|---|---|---|---|---|---|---|---|---|---|\n")
        for i, r in enumerate(results[:15]):
            bb_str = "Enabled" if r['bb_enabled'] else "Disabled"
            f.write(f"| {i+1} | {r['label']} | {r['stop_mult']}x | {r['trail_mult']}x | {bb_str} | ${r['total_net']:,.2f} | {r['trade_count']} | {r['win_rate']:.1%} | {r['profit_factor']:.2f} | ${r['avg_net']:,.2f} | {r['friction']:.1%} |\n")
            
        f.write("\n## Complete Results Sorted by PnL\n\n")
        f.write("| Configuration | Stop Mult | Trail Mult | BB Filter | Net PnL | Trades | Win% | Profit Factor | Avg Net |\n")
        f.write("|---|---|---|---|---|---|---|---|---|\n")
        for r in results:
            bb_str = "Enabled" if r['bb_enabled'] else "Disabled"
            f.write(f"| {r['label']} | {r['stop_mult']}x | {r['trail_mult']}x | {bb_str} | ${r['total_net']:,.2f} | {r['trade_count']} | {r['win_rate']:.1%} | {r['profit_factor']:.2f} | ${r['avg_net']:,.2f} |\n")

    print(f"Results successfully written to: {artifact_path}")

if __name__ == "__main__":
    # macOS Silicon optimization: Force main and spawned sub-processes to E-Cores
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
