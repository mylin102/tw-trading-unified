#!/usr/bin/env python3
# 2026-07-08 Gemini CLI: Run backtest comparing BB filter enabled vs disabled for TMF Spread strategy.

import os
import sys
import glob
import pandas as pd
from typing import Dict, List, Any

# Add project root to path
sys.path.append('.')

# Enable backtest flag to disable state recovery and disk I/O in strategy
os.environ["MTS_BACKTEST"] = "1"

from scripts.backtest_spread_v2 import SpreadBacktester, DATA_PATTERN

def run_backtest_with_config(files: List[str], bb_enabled: bool) -> Dict[str, Any]:
    # Use optimal parameters from sweep: ATR Stop 2.5x / Trail 2.0x
    config = {
        "params": {
            "allow_night_session": True,
            "regime": "WEAK",
            "entry_z": 2.5,
            "min_atr": 10.0,
            "atr_multiplier_stop": 2.5,
            "atr_multiplier_trail": 2.0,
            "release_stop_points": 999.0,
            "trail_distance_points": 999.0,
            "release_filter": {
                "bb_enabled": bb_enabled,
                "bb_period": 20,
                "bb_std_mult": 2.0,
                "sell_within_bb_upper": 8.0,
                "buy_within_bb_lower": 8.0,
                "emergency_bypass_enabled": True,
                "emergency_bypass_mult": 2.0
            }
        }
    }
    
    tester = SpreadBacktester("tmf_spread", config=config)
    
    # We want to force self._bb_enabled to the config value
    tester.strategy._bb_enabled = bb_enabled
    
    bb_period = 20
    bb_std = 2.0
    
    for f in files:
        df = pd.read_csv(f)
        if df.empty:
            continue
            
        # Calculate Bollinger Bands on the fly
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
            
        tester.run_on_df(df)
        
    return tester.get_metrics()

def main():
    files = sorted(glob.glob(DATA_PATTERN))
    if not files:
        print(f"No data files found matching {DATA_PATTERN}")
        return
        
    print(f"Running comparison backtest on {len(files)} files...")
    
    print("\n[1/2] Running Baseline (BB Filter Disabled)...")
    baseline = run_backtest_with_config(files, bb_enabled=False)
    
    print("\n[2/2] Running BB Filter Enabled...")
    bb_filter = run_backtest_with_config(files, bb_enabled=True)
    
    print("\n" + "="*90)
    print(f"{'Metric':<30} | {'Baseline (No BB Filter)':<25} | {'BB Filter Enabled':<25}")
    print("-" * 90)
    
    # Calculate PnL difference
    pnl_diff = bb_filter.get('total_net', 0) - baseline.get('total_net', 0)
    pnl_diff_pct = (pnl_diff / abs(baseline.get('total_net', 1))) * 100
    
    print(f"{'Total Net PnL':<30} | ${baseline.get('total_net', 0):>23,.2f} | ${bb_filter.get('total_net', 0):>23,.2f}")
    print(f"{'Total Gross PnL':<30} | ${baseline.get('total_gross', 0):>23,.2f} | ${bb_filter.get('total_gross', 0):>23,.2f}")
    print(f"{'Total Fees & Taxes':<30} | ${baseline.get('total_fees', 0) + baseline.get('total_taxes', 0):>23,.2f} | ${bb_filter.get('total_fees', 0) + bb_filter.get('total_taxes', 0):>23,.2f}")
    print(f"{'Trade Count':<30} | {baseline.get('trade_count', 0):>24} | {bb_filter.get('trade_count', 0):>24}")
    print(f"{'Win Rate':<30} | {baseline.get('win_rate', 0):>23.1%} | {bb_filter.get('win_rate', 0):>23.1%}")
    print(f"{'Profit Factor':<30} | {baseline.get('profit_factor', 0):>24.2f} | {bb_filter.get('profit_factor', 0):>24.2f}")
    print(f"{'Avg Net per Trade':<30} | ${baseline.get('avg_net', 0):>23,.2f} | ${bb_filter.get('avg_net', 0):>23,.2f}")
    print("="*90)
    
    print(f"\nAnalysis Summary:")
    print(f"- Net PnL Change: ${pnl_diff:+,.2f} ({pnl_diff_pct:+.1f}%)")
    if pnl_diff > 0:
        print("- The Bollinger Band Filter successfully optimized exit timing and increased total net profit!")
    else:
        print("- The Bollinger Band Filter reduced profits or did not improve overall results in this scenario.")

if __name__ == "__main__":
    # macOS Silicon optimization: Force main and spawned sub-processes to E-Cores
    if sys.platform == "darwin":
        os.system(f"taskpolicy -b -p {os.getpid()}")
    main()
