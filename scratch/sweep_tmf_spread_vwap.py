#!/usr/bin/env python3
# 2026-07-08 Gemini CLI: Sweep VWAP Exit configurations (enabled vs disabled, and tighten ratios).

import os
import sys
import glob
import pandas as pd
from typing import Dict, List, Any
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.append('.')
os.environ["MTS_BACKTEST"] = "1"

from scripts.backtest_spread_v2 import SpreadBacktester, DATA_PATTERN

def run_scenario(strat_name: str, dfs: List[pd.DataFrame], config: Dict[str, Any]) -> Dict[str, Any]:
    try:
        tester = SpreadBacktester(strat_name, config=config)
        # Force config values directly into strategy instance parameters
        params = config["params"]
        tester.strategy._params = params
        
        for df in dfs:
            tester.run_on_df(df)
        return tester.get_metrics()
    except Exception as e:
        print(f"Error running configuration: {e}", file=sys.stderr)
        return {}

def run_single_config(args) -> Dict[str, Any]:
    strat_name, dfs, config, label, stop_mult, trail_mult, vwap_enabled, tighten_ratio = args
    metrics = run_scenario(strat_name, dfs, config)
    if metrics and metrics.get("trade_count", 0) > 0 and "total_net" in metrics:
        metrics["label"] = label
        metrics["stop_mult"] = stop_mult
        metrics["trail_mult"] = trail_mult
        metrics["vwap_enabled"] = vwap_enabled
        metrics["tighten_ratio"] = tighten_ratio
        return metrics
    return {}

def main():
    files = sorted(glob.glob(DATA_PATTERN))
    if not files:
        print(f"No data files found matching {DATA_PATTERN}")
        return

    print(f"Loaded {len(files)} historical spread CSV files...")
    
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
        
        # Calculate Simulated VWAPs (Daily expanding mean)
        df["near_vwap"] = df["Close_near"].expanding().mean()
        df["far_vwap"] = df["Close_far"].expanding().mean()
        df["vwap"] = df["near_vwap"]
        
        df["sqz_on"] = True
        
        ts_col = next((c for c in ["ts", "timestamp", "datetime"] if c in df.columns), None)
        if ts_col:
            df[ts_col] = pd.to_datetime(df[ts_col])
            df = df.set_index(ts_col)
        dfs.append(df)

    tasks = []
    # Test top stops from first sweep (1.0x and 2.5x ATR stops)
    stops = [1.0, 2.5]
    trail_mult = 2.0
    
    # Sweep configurations
    # 1. VWAP Exit Disabled (Control group)
    # 2. VWAP Exit Enabled with various tightening ratios
    ratios = [0.0, 0.1, 0.2, 0.3, 0.5, 0.8] # 0.0 means immediate exit

    for stop_mult in stops:
        # Add control task (VWAP Disabled)
        config_disabled = {
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
                    "bb_enabled": True,
                    "bb_period": bb_period,
                    "bb_std_mult": bb_std,
                    "sell_within_bb_upper": 0.10,
                    "buy_within_bb_lower": 0.10,
                    "emergency_bypass_enabled": True,
                    "emergency_bypass_mult": 2.0
                },
                "vwap_exit": {
                    "enabled": False,
                    "tighten_ratio": 1.0
                }
            }
        }
        tasks.append(("tmf_spread", dfs, config_disabled, f"Stop {stop_mult}x/Trail {trail_mult}x (VWAP Disabled)", stop_mult, trail_mult, False, 1.0))
        
        # Add VWAP Exit Enabled tasks
        for ratio in ratios:
            config_enabled = {
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
                        "bb_enabled": True,
                        "bb_period": bb_period,
                        "bb_std_mult": bb_std,
                        "sell_within_bb_upper": 0.10,
                        "buy_within_bb_lower": 0.10,
                        "emergency_bypass_enabled": True,
                        "emergency_bypass_mult": 2.0
                    },
                    "vwap_exit": {
                        "enabled": True,
                        "tighten_ratio": ratio
                    }
                }
            }
            label = f"Stop {stop_mult}x/Trail {trail_mult}x (VWAP Exit Ratio {ratio})"
            tasks.append(("tmf_spread", dfs, config_enabled, label, stop_mult, trail_mult, True, ratio))

    results = []
    print(f"Total configurations to evaluate: {len(tasks)}. Running in parallel (E-Cores)...")

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = {executor.submit(run_single_config, task): task for task in tasks}
        for future in as_completed(futures):
            metrics = future.result()
            if metrics:
                results.append(metrics)

    results = sorted(results, key=lambda x: x.get("total_net", -999999), reverse=True)

    print("\nSweep Complete! Saving results to Markdown artifact...")
    
    artifact_dir = os.environ.get(
        "ARTIFACT_DIR", 
        "/Users/mylin/.gemini/antigravity-cli/brain/487d8f97-542a-4e2d-80e4-ddd146d2a064"
    )
    artifact_path = os.path.join(artifact_dir, "tmf_spread_vwap_exit_results.md")
    
    os.makedirs(os.path.dirname(artifact_path), exist_ok=True)
    
    with open(artifact_path, "w") as f:
        f.write("# TMF Spread VWAP-based Trailing Exit Sweep Results\n\n")
        f.write("Backtest evaluation period: All historical calendar spread days.\n")
        f.write("Testing VWAP-based trailing stop tightening (Adaptive VWAP Exit) for remaining leg.\n\n")
        
        f.write("## Sweep Configurations Ranked by Net PnL\n\n")
        f.write("| Rank | Configuration | Stop Mult | Trail Mult | VWAP Exit | Tighten Ratio | Net PnL | Trades | Win% | Profit Factor | Avg Net | \n")
        f.write("|---|---|---|---|---|---|---|---|---|---| \n")
        for i, r in enumerate(results):
            vwap_status = "ENABLED" if r['vwap_enabled'] else "DISABLED"
            ratio_str = f"{r['tighten_ratio']}" if r['vwap_enabled'] else "N/A"
            f.write(f"| {i+1} | {r['label']} | {r['stop_mult']}x | {r['trail_mult']}x | {vwap_status} | {ratio_str} | ${r['total_net']:,.2f} | {r['trade_count']} | {r['win_rate']:.1%} | {r['profit_factor']:.2f} | ${r['avg_net']:,.2f} |\n")

    print(f"Results successfully written to: {artifact_path}")

if __name__ == "__main__":
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
