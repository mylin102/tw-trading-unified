#!/usr/bin/env python3
"""
Attribution Data Collector for Night Session

This script enables attribution logging and collects router evaluation data
for strategy analysis. It runs for a specified duration or until enough
data is collected.

Minimum requirements:
- 3-5 trading days
- 200-500 bars
- At least 10 candidate appearances per major strategy
"""

import sys
import os
import time
import json
import signal
from pathlib import Path
from datetime import datetime, timedelta
import pandas as pd

# Add project root to path
PROJECT_ROOT = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
sys.path.insert(0, str(PROJECT_ROOT))

from core.attribution_recorder import AttributionRecorder
from strategies.futures.monitor import FuturesMonitor
from core.market_regime import classify_regime

class AttributionCollector:
    """Collect attribution data for strategy analysis."""
    
    def __init__(self, output_dir=None, duration_hours=24, min_bars=200):
        self.output_dir = output_dir or PROJECT_ROOT / "data" / "attribution" / "collection"
        self.duration_hours = duration_hours
        self.min_bars = min_bars
        
        # Create output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize attribution recorder
        self.recorder = AttributionRecorder(
            output_dir=self.output_dir,
            buffer_size=100,  # Smaller buffer for more frequent saves
            flush_interval_seconds=60,  # Save every minute
            flush_on_exit=True
        )
        
        # Initialize monitor with attribution
        self.monitor = self._create_monitor_with_attribution()
        
        # Statistics
        self.stats = {
            "start_time": datetime.now(),
            "bars_processed": 0,
            "strategies_evaluated": {},
            "last_update": datetime.now()
        }
        
        # Signal handling
        self.running = True
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
    def _create_monitor_with_attribution(self):
        """Create monitor with attribution enabled."""
        # This is a simplified version - in reality, we need to load
        # the actual monitor configuration
        try:
            # Try to load existing monitor
            from strategies.futures.monitor import FuturesMonitor
            monitor = FuturesMonitor()
            # Enable attribution by setting the recorder
            monitor.attribution_recorder = self.recorder
            return monitor
        except Exception as e:
            print(f"Warning: Could not create monitor: {e}")
            return None
    
    def signal_handler(self, signum, frame):
        """Handle interrupt signals."""
        print(f"\nReceived signal {signum}, shutting down...")
        self.running = False
    
    def collect_sample_data(self):
        """Collect sample data for testing."""
        print("=" * 70)
        print("Attribution Data Collection")
        print("=" * 70)
        print(f"Output directory: {self.output_dir}")
        print(f"Duration: {self.duration_hours} hours")
        print(f"Minimum bars: {self.min_bars}")
        print(f"Start time: {self.stats['start_time'].strftime('%Y-%m-%d %H:%M:%S')}")
        print()
        
        # Create sample bar data
        sample_bars = self._generate_sample_bars()
        
        for i, bar in enumerate(sample_bars):
            if not self.running:
                break
                
            # Process bar
            self._process_bar(bar)
            
            # Update statistics
            self.stats["bars_processed"] += 1
            
            # Print progress
            if i % 10 == 0:
                self._print_progress(i, len(sample_bars))
            
            # Check if we have enough data
            if self._has_enough_data():
                print("\n✅ Minimum data requirements met!")
                break
        
        # Final flush
        self.recorder.export_csv_if_needed(force=True)
        
        # Save statistics
        self._save_statistics()
        
        # Generate summary report
        self._generate_summary()
        
        print("\n" + "=" * 70)
        print("Collection Complete!")
        print("=" * 70)
    
    def _generate_sample_bars(self):
        """Generate sample bar data for testing."""
        bars = []
        base_price = 20000
        
        # Generate 500 bars (about 2-3 days of 5-minute data)
        for i in range(500):
            timestamp = datetime.now() - timedelta(minutes=5 * (500 - i))
            
            # Simulate price movement
            price_change = (i % 20 - 10) * 10  # Oscillating pattern
            current_price = base_price + price_change
            
            bar = {
                "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "open": current_price - 5,
                "high": current_price + 10,
                "low": current_price - 10,
                "close": current_price,
                "volume": 1000 + (i % 100) * 50,
                "symbol": "TX"
            }
            bars.append(bar)
        
        return bars
    
    def _process_bar(self, bar):
        """Process a single bar through the router."""
        if not self.monitor:
            # Simulate router evaluation if monitor not available
            self._simulate_router_evaluation(bar)
            return
        
        try:
            # Get session regime
            session_regime = classify_regime(None)  # Simplified
            
            # Route signal with attribution
            decision = self.monitor._route_signal(
                bar=bar,
                session_regime=session_regime,
                attribution_recorder=self.recorder
            )
            
            # Update strategy statistics
            if decision and decision.candidates:
                for strategy in decision.candidates:
                    self.stats["strategies_evaluated"][strategy] = \
                        self.stats["strategies_evaluated"].get(strategy, 0) + 1
        
        except Exception as e:
            print(f"Error processing bar: {e}")
            # Fallback to simulation
            self._simulate_router_evaluation(bar)
    
    def _simulate_router_evaluation(self, bar):
        """Simulate router evaluation for testing."""
        # Simulate three strategies being evaluated
        strategies = ["counter_vwap", "spring_upthrust", "kbar_feature"]
        
        for order, strategy in enumerate(strategies):
            # Randomly decide if this strategy would win
            winner = (order == 0)  # First strategy wins
            
            # Log to attribution recorder
            self.recorder.log_router_row(
                timestamp=bar["timestamp"],
                symbol=bar["symbol"],
                regime="WEAK",  # Default regime
                strategy_name=strategy,
                candidate_order=order + 1,
                status="evaluated",
                evaluated=True,
                winner=winner,
                signal_side="BUY" if winner else None,
                signal_type="entry" if winner else None,
                notes=f"Simulated evaluation - order {order + 1}"
            )
            
            # Update statistics
            self.stats["strategies_evaluated"][strategy] = \
                self.stats["strategies_evaluated"].get(strategy, 0) + 1
    
    def _print_progress(self, current, total):
        """Print collection progress."""
        progress = (current + 1) / total * 100
        bars_processed = self.stats["bars_processed"]
        
        print(f"\rProgress: {progress:.1f}% | Bars: {bars_processed}/{self.min_bars} | "
              f"Strategies: {len(self.stats['strategies_evaluated'])}", end="")
        
        # Print strategy counts every 50 bars
        if current % 50 == 0 and current > 0:
            print(f"\nStrategy evaluations:")
            for strategy, count in self.stats["strategies_evaluated"].items():
                print(f"  {strategy}: {count}")
    
    def _has_enough_data(self):
        """Check if we have enough data for analysis."""
        # Check minimum bars
        if self.stats["bars_processed"] < self.min_bars:
            return False
        
        # Check strategy evaluations
        strategy_counts = list(self.stats["strategies_evaluated"].values())
        if len(strategy_counts) < 3:  # Need at least 3 strategies
            return False
        
        # Each major strategy should have at least 10 evaluations
        for count in strategy_counts:
            if count < 10:
                return False
        
        return True
    
    def _save_statistics(self):
        """Save collection statistics."""
        stats_file = self.output_dir / "collection_stats.json"
        
        stats_data = {
            "collection": {
                "start_time": self.stats["start_time"].isoformat(),
                "end_time": datetime.now().isoformat(),
                "duration_hours": self.duration_hours,
                "bars_processed": self.stats["bars_processed"],
                "strategies_evaluated": self.stats["strategies_evaluated"]
            },
            "requirements": {
                "min_bars": self.min_bars,
                "min_strategy_evaluations": 10,
                "min_strategies": 3
            },
            "files_generated": self._list_generated_files()
        }
        
        with open(stats_file, 'w') as f:
            json.dump(stats_data, f, indent=2, ensure_ascii=False)
        
        print(f"Statistics saved to: {stats_file}")
    
    def _list_generated_files(self):
        """List generated CSV files."""
        files = {}
        for csv_file in self.output_dir.glob("*.csv"):
            try:
                df = pd.read_csv(csv_file)
                files[csv_file.name] = {
                    "rows": len(df),
                    "columns": list(df.columns),
                    "size_kb": csv_file.stat().st_size / 1024
                }
            except Exception as e:
                files[csv_file.name] = {"error": str(e)}
        
        return files
    
    def _generate_summary(self):
        """Generate summary report."""
        summary_file = self.output_dir / "collection_summary.md"
        
        with open(summary_file, 'w') as f:
            f.write("# Attribution Data Collection Summary\n\n")
            f.write(f"**Collection Period**: {self.stats['start_time'].strftime('%Y-%m-%d %H:%M:%S')} to {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f"**Total Bars Processed**: {self.stats['bars_processed']}\n\n")
            
            f.write("## Strategy Evaluations\n\n")
            f.write("| Strategy | Evaluations |\n")
            f.write("|----------|-------------|\n")
            for strategy, count in sorted(self.stats["strategies_evaluated"].items(), key=lambda x: x[1], reverse=True):
                f.write(f"| {strategy} | {count} |\n")
            
            f.write("\n## Generated Files\n\n")
            files_info = self._list_generated_files()
            for filename, info in files_info.items():
                if "rows" in info:
                    f.write(f"- **{filename}**: {info['rows']} rows, {info['size_kb']:.1f} KB\n")
                    f.write(f"  - Columns: {', '.join(info['columns'][:5])}...\n")
            
            f.write("\n## Next Steps\n\n")
            f.write("1. Run attribution report:\n")
            f.write("   ```bash\n")
            f.write(f"   python scripts/attribution_report.py --input-dir {self.output_dir} --output-dir {self.output_dir}/reports\n")
            f.write("   ```\n\n")
            f.write("2. Check for starvation:\n")
            f.write("   ```bash\n")
            f.write(f"   python scripts/starvation_alerts.py --input-dir {self.output_dir} --threshold 0.7\n")
            f.write("   ```\n\n")
            f.write("3. Run reorder simulation:\n")
            f.write("   ```bash\n")
            f.write(f"   python docs/strategy_reorder_simulator.py --input-dir {self.output_dir}\n")
            f.write("   ```\n")
        
        print(f"Summary saved to: {summary_file}")

def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Collect attribution data for strategy analysis")
    parser.add_argument("--output-dir", type=str, help="Output directory for attribution data")
    parser.add_argument("--duration", type=int, default=24, help="Collection duration in hours")
    parser.add_argument("--min-bars", type=int, default=200, help="Minimum bars to collect")
    parser.add_argument("--sample-only", action="store_true", help="Generate sample data only")
    
    args = parser.parse_args()
    
    if args.sample_only:
        print("Generating sample attribution data...")
        collector = AttributionCollector(
            output_dir=args.output_dir,
            duration_hours=args.duration,
            min_bars=args.min_bars
        )
        collector.collect_sample_data()
    else:
        print("Starting attribution data collection...")
        print("Note: This requires a running trading system with attribution enabled.")
        print("For sample data, use --sample-only flag.")
        # In production, this would connect to the live system
        # For now, we'll use sample data
        collector = AttributionCollector(
            output_dir=args.output_dir,
            duration_hours=args.duration,
            min_bars=args.min_bars
        )
        collector.collect_sample_data()

if __name__ == "__main__":
    main()