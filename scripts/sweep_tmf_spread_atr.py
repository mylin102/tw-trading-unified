#!/usr/bin/env python3
# 2026-06-25 Hermes Agent: Script to sweep ATR and fixed-point multipliers for TMF spread strategy
import os
import sys
import glob
import pandas as pd
from typing import Dict, List, Any
from concurrent.futures import ProcessPoolExecutor, as_completed

# Add project root to path
sys.path.append('.')

# 2026-06-25 Gemini CLI: Enable backtest flag to disable state recovery and disk I/O in strategy
os.environ["MTS_BACKTEST"] = "1"

from scripts.backtest_spread_v2 import SpreadBacktester, DATA_PATTERN

def run_scenario(strat_name: str, dfs: List[pd.DataFrame], config: Dict[str, Any]) -> Dict[str, Any]:
    try:
        tester = SpreadBacktester(strat_name, config=config)
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
    strat_name, dfs, config, label, stop_val, trail_val, mode = args
    metrics = run_scenario(strat_name, dfs, config)
    if metrics and metrics.get("trade_count", 0) > 0 and "total_net" in metrics:
        total_gross = metrics.get("total_gross", 0.0)
        total_costs = metrics.get("total_fees", 0.0) + metrics.get("total_taxes", 0.0)
        metrics["label"] = label
        metrics["mode"] = mode
        metrics["friction"] = total_costs / abs(total_gross if total_gross != 0 else 1)
        if mode == "ATR":
            metrics["stop_mult"] = stop_val
            metrics["trail_mult"] = trail_val
        else:
            metrics["stop_pts"] = stop_val
            metrics["trail_pts"] = trail_val
        return metrics
    return {}

def main():
    files = sorted(glob.glob(DATA_PATTERN))
    if not files:
        print(f"No data files found matching {DATA_PATTERN}")
        return

    strat_name = "tmf_spread"
    tasks = []

    print(f"Loaded {len(files)} historical spread CSV files. Pre-loading into memory...")
    dfs = []
    for f in files:
        df = pd.read_csv(f)
        if df.empty:
            continue
        ts_col = next((c for c in ["ts", "timestamp", "datetime"] if c in df.columns), None)
        if ts_col:
            df[ts_col] = pd.to_datetime(df[ts_col])
            df = df.set_index(ts_col)
        dfs.append(df)
    print(f"Successfully pre-loaded {len(dfs)} DataFrames. Preparing grid search tasks...")

    # Grid parameters (Updated 2026-06-30 Gemini CLI to include tight trail stops down to 0.1)
    atr_stops = [1.0, 1.5, 2.0, 2.5]
    atr_trails = [0.1, 0.2, 0.3, 0.4, 0.6, 0.8, 1.0, 1.5, 2.0]

    for stop_mult in atr_stops:
        for trail_mult in atr_trails:
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
                }
            }
            label = f"ATR Stop {stop_mult}x / Trail {trail_mult}x"
            tasks.append((strat_name, dfs, config, label, stop_mult, trail_mult, "ATR"))

    fixed_stops = [10, 15, 20, 25, 30]
    fixed_trails = [10, 20, 30, 40, 50, 60, 80, 100]

    for f_stop in fixed_stops:
        for f_trail in fixed_trails:
            config = {
                "params": {
                    "allow_night_session": True,
                    "regime": "WEAK",
                    "entry_z": 2.5,
                    "min_atr": 0.0,
                    "atr_multiplier_stop": 0.0,
                    "atr_multiplier_trail": 0.0,
                    "release_stop_points": f_stop,
                    "trail_distance_points": f_trail,
                }
            }
            label = f"Fixed Stop {f_stop}pt / Trail {f_trail}pt"
            tasks.append((strat_name, dfs, config, label, f_stop, f_trail, "Fixed"))

    results = []
    print(f"Total tasks scheduled: {len(tasks)}. Executing in parallel...")

    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(run_single_config, task): task for task in tasks}
        completed_count = 0
        for future in as_completed(futures):
            metrics = future.result()
            completed_count += 1
            if completed_count % 10 == 0 or completed_count == len(tasks):
                print(f"Progress: {completed_count}/{len(tasks)} configurations evaluated.")
            if metrics:
                results.append(metrics)

    results = sorted(results, key=lambda x: x.get("total_net", -999999), reverse=True)

    print("\nSweep Complete! Saving results...")
    
    artifact_path = os.path.join(
        os.getenv("ARTIFACT_DIR", "/Users/mylin/.gemini/antigravity-cli/brain/7e4cfbee-eaf2-4a7c-850a-ffc21c669a29"),
        "tmf_spread_sweep_results.md"
    )
    
    os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
    
    with open(artifact_path, "w") as f:
        f.write("# TMF Spread Parameter Sweep Results\n\n")
        f.write("Backtest period: All loaded calendar days. Sorted by Total Net PnL (descending).\n\n")
        
        f.write("## Top 15 Configurations\n\n")
        f.write("| Rank | Configuration | Mode | Net PnL | Trades | Win% | Profit Factor | Avg Net | Friction |\n")
        f.write("|---|---|---|---|---|---|---|---|---|\n")
        for i, r in enumerate(results[:15]):
            f.write(f"| {i+1} | {r['label']} | {r['mode']} | ${r['total_net']:,.2f} | {r['trade_count']} | {r['win_rate']:.1%} | {r['profit_factor']:.2f} | ${r['avg_net']:,.2f} | {r['friction']:.1%} |\n")
        
        f.write("\n## Complete ATR-based Sweeps\n\n")
        f.write("| Stop Mult | Trail Mult | Net PnL | Trades | Win% | Profit Factor | Avg Net |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        atr_res = sorted([r for r in results if r['mode'] == "ATR"], key=lambda x: x.get("total_net", -999999), reverse=True)
        for r in atr_res:
            f.write(f"| {r['stop_mult']}x | {r['trail_mult']}x | ${r['total_net']:,.2f} | {r['trade_count']} | {r['win_rate']:.1%} | {r['profit_factor']:.2f} | ${r['avg_net']:,.2f} |\n")

        f.write("\n## Complete Fixed-point Sweeps\n\n")
        f.write("| Stop Pts | Trail Pts | Net PnL | Trades | Win% | Profit Factor | Avg Net |\n")
        f.write("|---|---|---|---|---|---|---|\n")
        fixed_res = sorted([r for r in results if r['mode'] == "Fixed"], key=lambda x: x.get("total_net", -999999), reverse=True)
        for r in fixed_res:
            f.write(f"| {r['stop_pts']}pt | {r['trail_pts']}pt | ${r['total_net']:,.2f} | {r['trade_count']} | {r['win_rate']:.1%} | {r['profit_factor']:.2f} | ${r['avg_net']:,.2f} |\n")

    print(f"Results successfully written to: {artifact_path}")

if __name__ == "__main__":
    # macOS Silicon optimization: Force main and spawned sub-processes to E-Cores
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
