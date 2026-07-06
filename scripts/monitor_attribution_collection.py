#!/usr/bin/env python3
"""
Monitor trading system for attribution data collection.
"""

import sys
import time
import os
from pathlib import Path
from datetime import datetime

PROJECT_ROOT = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")

def monitor_attribution_data(timeout_minutes=5):
    """Monitor attribution data collection."""
    
    print("=" * 70)
    print("Attribution Data Collection Monitor")
    print("=" * 70)
    print(f"Start time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Timeout: {timeout_minutes} minutes")
    print()
    
    # Check initial state
    data_dir = PROJECT_ROOT / 'data' / 'attribution' / 'night_session'
    initial_count = 0
    if data_dir.exists():
        router_file = data_dir / 'router_evaluation_log.csv'
        if router_file.exists():
            with open(router_file, 'r') as f:
                lines = f.readlines()
                initial_count = len(lines) - 1  # Exclude header
    
    print(f"Initial attribution data: {initial_count} rows")
    
    # Monitor for new data
    start_time = time.time()
    timeout_seconds = timeout_minutes * 60
    
    last_count = initial_count
    check_interval = 10  # seconds
    
    while time.time() - start_time < timeout_seconds:
        current_time = time.time()
        elapsed_minutes = (current_time - start_time) / 60
        
        # Check for new data
        current_count = 0
        if data_dir.exists():
            router_file = data_dir / 'router_evaluation_log.csv'
            if router_file.exists():
                with open(router_file, 'r') as f:
                    lines = f.readlines()
                    current_count = len(lines) - 1  # Exclude header
        
        # Calculate rate
        new_rows = current_count - last_count
        if new_rows > 0:
            rate = new_rows / check_interval  # rows per second
            print(f"[{datetime.now().strftime('%H:%M:%S')}] +{new_rows} rows "
                  f"(total: {current_count}, rate: {rate:.1f} rows/sec)")
            
            # Check if we have real data
            if current_count > 0:
                check_data_quality(data_dir)
        
        last_count = current_count
        
        # Check trading logs
        check_trading_logs()
        
        # Print status
        if int(elapsed_minutes) % 1 == 0 and int(elapsed_minutes) > 0:
            print(f"\n📊 Status after {int(elapsed_minutes)} minutes:")
            print(f"  Total rows: {current_count}")
            print(f"  New rows this minute: {current_count - initial_count}")
            print(f"  Data collection active: {'✅' if new_rows > 0 else '❌'}")
        
        time.sleep(check_interval)
    
    print("\n" + "=" * 70)
    print("Monitoring Complete")
    print("=" * 70)
    
    # Final report
    final_count = 0
    if data_dir.exists():
        router_file = data_dir / 'router_evaluation_log.csv'
        if router_file.exists():
            with open(router_file, 'r') as f:
                lines = f.readlines()
                final_count = len(lines) - 1
    
    total_new = final_count - initial_count
    print(f"Total monitoring time: {timeout_minutes} minutes")
    print(f"Initial rows: {initial_count}")
    print(f"Final rows: {final_count}")
    print(f"New rows collected: {total_new}")
    print(f"Collection rate: {total_new / timeout_minutes:.1f} rows/minute")
    
    if total_new > 0:
        print("\n✅ Data collection successful!")
        generate_summary_report(data_dir, initial_count, final_count)
    else:
        print("\n❌ No new data collected")
        print("Possible issues:")
        print("  1. Trading system not processing bars")
        print("  2. Router not enabled in configuration")
        print("  3. Attribution recorder not integrated")
        print("  4. System in maintenance or error state")

def check_data_quality(data_dir):
    """Check quality of attribution data."""
    try:
        import pandas as pd
        
        router_file = data_dir / 'router_evaluation_log.csv'
        if not router_file.exists():
            return
        
        df = pd.read_csv(router_file)
        
        # Check for simulated data
        if 'notes' in df.columns:
            notes = df['notes'].astype(str)
            simulated_count = notes.str.contains('simulated|test|mock', case=False).sum()
            if simulated_count > 0:
                print(f"  ⚠️  Found {simulated_count} simulated rows")
        
        # Check strategy distribution
        if 'strategy_name' in df.columns:
            strategies = df['strategy_name'].value_counts()
            if len(strategies) >= 3:
                print(f"  ✅ {len(strategies)} strategies being evaluated")
        
        # Check timestamp recency
        if 'timestamp' in df.columns:
            try:
                df['timestamp'] = pd.to_datetime(df['timestamp'])
                latest = df['timestamp'].max()
                age = (pd.Timestamp.now() - latest).total_seconds() / 60
                if age < 5:
                    print(f"  ✅ Data is recent ({age:.1f} minutes old)")
            except:
                pass
                
    except Exception as e:
        print(f"  ⚠️  Error checking data quality: {e}")

def check_trading_logs():
    """Check trading system logs for activity."""
    log_file = PROJECT_ROOT / 'logs' / 'pm2-trading-out-3.log'
    
    if not log_file.exists():
        return
    
    try:
        # Get last 10 lines
        with open(log_file, 'r') as f:
            lines = f.readlines()[-10:]
        
        # Check for key patterns
        patterns = {
            'New Bar': '📊',
            'candidates': '🔄',
            'entry': '🚀',
            'signal': '📶',
            'MTX updated': '💹',
            'error': '❌',
            'warning': '⚠️'
        }
        
        for line in lines:
            for pattern, icon in patterns.items():
                if pattern in line:
                    timestamp = line.split(']')[0].replace('[', '')[:19]
                    message = line.split(']')[-1].strip()[:50]
                    print(f"  {icon} [{timestamp}] {message}...")
                    break
                    
    except Exception as e:
        pass

def generate_summary_report(data_dir, initial_count, final_count):
    """Generate summary report."""
    try:
        import pandas as pd
        import yaml
        
        print("\n" + "=" * 70)
        print("Attribution Data Summary")
        print("=" * 70)
        
        # Load data
        router_file = data_dir / 'router_evaluation_log.csv'
        df = pd.read_csv(router_file)
        
        # Basic stats
        print(f"Total rows: {len(df)}")
        print(f"Collection period: {df['timestamp'].min()} to {df['timestamp'].max()}")
        
        # Strategy analysis
        if 'strategy_name' in df.columns:
            print("\nStrategy Evaluation Counts:")
            strategy_counts = df['strategy_name'].value_counts()
            for strategy, count in strategy_counts.items():
                percentage = count / len(df) * 100
                print(f"  {strategy}: {count} ({percentage:.1f}%)")
        
        # Winner analysis
        if 'winner' in df.columns:
            win_counts = df['winner'].value_counts()
            print(f"\nWin counts: {dict(win_counts)}")
        
        # Regime analysis
        if 'regime' in df.columns:
            regime_counts = df['regime'].value_counts()
            print(f"\nRegime distribution: {dict(regime_counts)}")
        
        # Check configuration
        config_file = PROJECT_ROOT / 'config' / 'futures.yaml'
        if config_file.exists():
            with open(config_file, 'r') as f:
                config = yaml.safe_load(f)
            
            print(f"\nConfiguration:")
            print(f"  auto_select: {config.get('strategy', {}).get('auto_select', 'N/A')}")
            print(f"  strategy_list: {config.get('strategy', {}).get('strategy_list', 'N/A')}")
        
        # Recommendations
        print("\n" + "=" * 70)
        print("Recommendations")
        print("=" * 70)
        
        if len(df) >= 200:
            print("✅ Sufficient data for analysis")
            print("   Run: python3 scripts/attribution_report.py")
            print("   Run: python3 docs/strategy_reorder_simulator.py")
        else:
            print("⚠️  Need more data (target: 200+ rows)")
            print("   Continue monitoring for 1-2 hours")
        
        if 'winner' in df.columns and df['winner'].notna().any():
            print("✅ Real strategy competition detected")
        else:
            print("⚠️  No winner data - check router implementation")
        
    except Exception as e:
        print(f"Error generating report: {e}")

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Monitor attribution data collection")
    parser.add_argument("--timeout", type=int, default=5, help="Monitoring timeout in minutes")
    
    args = parser.parse_args()
    
    try:
        monitor_attribution_data(timeout_minutes=args.timeout)
    except KeyboardInterrupt:
        print("\n\nMonitoring interrupted by user")
        print("Current attribution data preserved")
    except Exception as e:
        print(f"Error: {e}")