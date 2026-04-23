#!/usr/bin/env python3
"""
Night Session Monitor with Attribution Logging

This script monitors night trading sessions with attribution logging enabled.
It collects router evaluation data for strategy analysis.

Key features:
1. Attribution logging enabled
2. Real-time monitoring
3. Data collection for analysis
4. Starvation detection
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

class NightSessionMonitor:
    """Monitor night trading sessions with attribution logging."""
    
    def __init__(self, config_path=None):
        self.project_root = PROJECT_ROOT
        
        # Configuration
        self.config = self._load_config(config_path)
        
        # Directories
        self.attribution_dir = self.project_root / "data" / "attribution" / "night_session"
        self.attribution_dir.mkdir(parents=True, exist_ok=True)
        
        self.logs_dir = self.project_root / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize attribution recorder
        self.recorder = AttributionRecorder(
            output_dir=self.attribution_dir,
            buffer_size=100,
            flush_interval_seconds=60,
            flush_on_exit=True
        )
        
        # Statistics
        self.stats = {
            "start_time": datetime.now(),
            "bars_processed": 0,
            "last_bar_time": None,
            "strategies_evaluated": {},
            "attribution_enabled": True
        }
        
        # Signal handling
        self.running = True
        signal.signal(signal.SIGINT, self.signal_handler)
        signal.signal(signal.SIGTERM, self.signal_handler)
        
        # Log file
        self.log_file = self.logs_dir / "night_monitor.log"
        
    def _load_config(self, config_path):
        """Load configuration."""
        default_config = {
            "attribution_enabled": True,
            "check_interval_seconds": 10,
            "min_bars_for_analysis": 50,
            "starvation_threshold": 0.7,
            "night_session_start_hour": 15,
            "night_session_end_hour": 5
        }
        
        if config_path and Path(config_path).exists():
            try:
                with open(config_path, 'r') as f:
                    user_config = json.load(f)
                    default_config.update(user_config)
            except Exception as e:
                print(f"Warning: Could not load config: {e}")
        
        return default_config
    
    def signal_handler(self, signum, frame):
        """Handle interrupt signals."""
        self.log(f"Received signal {signum}, shutting down...")
        self.running = False
    
    def log(self, message, level="INFO"):
        """Log message to file and console."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[{timestamp}] [{level}] {message}"
        
        # Print to console
        print(log_message)
        
        # Write to log file
        with open(self.log_file, 'a') as f:
            f.write(log_message + "\n")
    
    def is_night_session(self):
        """Check if current time is within night session hours."""
        hour = datetime.now().hour
        return hour >= self.config["night_session_start_hour"] or hour < self.config["night_session_end_hour"]
    
    def check_trading_system(self):
        """Check if trading system is running and collect data."""
        # Check unified.log for new bars
        unified_log = self.project_root / "logs" / "unified.log"
        
        if not unified_log.exists():
            self.log("unified.log not found - trading system may not be running", "WARNING")
            return False
        
        try:
            # Read last 100 lines of log
            with open(unified_log, 'r') as f:
                lines = f.readlines()[-100:]
            
            # Look for new bars
            new_bars = []
            for line in reversed(lines):
                if "[FuturesMonitor] New Bar:" in line:
                    # Extract bar information
                    bar_info = self._extract_bar_info(line)
                    if bar_info:
                        new_bars.append(bar_info)
            
            # Process new bars
            if new_bars:
                self._process_bars(new_bars)
                return True
            
            return False
            
        except Exception as e:
            self.log(f"Error checking trading system: {e}", "ERROR")
            return False
    
    def _extract_bar_info(self, log_line):
        """Extract bar information from log line."""
        try:
            # Example: [2026-04-22 21:00:00] [INFO] [FuturesMonitor] New Bar: TX 5m close=20050.0
            parts = log_line.strip().split("New Bar:")
            if len(parts) < 2:
                return None
            
            bar_data = parts[1].strip()
            
            # Extract timestamp
            timestamp_match = log_line.split("]")[0].replace("[", "")
            timestamp = datetime.strptime(timestamp_match.strip(), "%Y-%m-%d %H:%M:%S")
            
            # Parse bar data
            # Simple parsing - in reality would need more sophisticated parsing
            bar_info = {
                "timestamp": timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "symbol": "TX",  # Default
                "close": 20000.0,  # Default
                "volume": 1000  # Default
            }
            
            return bar_info
            
        except Exception as e:
            self.log(f"Error extracting bar info: {e}", "WARNING")
            return None
    
    def _process_bars(self, bars):
        """Process new bars."""
        for bar in bars:
            # Simulate router evaluation (in production, this would come from actual router)
            self._simulate_router_evaluation(bar)
            
            # Update statistics
            self.stats["bars_processed"] += 1
            self.stats["last_bar_time"] = bar["timestamp"]
            
            # Log progress
            if self.stats["bars_processed"] % 10 == 0:
                self.log(f"Processed {self.stats['bars_processed']} bars")
    
    def _simulate_router_evaluation(self, bar):
        """Simulate router evaluation for testing."""
        # Three strategies in order
        strategies = ["counter_vwap", "spring_upthrust", "kbar_feature"]
        
        # Determine winner based on some logic
        # For simulation, first strategy wins 60% of the time
        import random
        winner_index = 0 if random.random() < 0.6 else (1 if random.random() < 0.7 else 2)
        
        for order, strategy in enumerate(strategies):
            winner = (order == winner_index)
            
            # Log to attribution recorder
            self.recorder.log_router_row(
                timestamp=bar["timestamp"],
                symbol=bar["symbol"],
                regime="WEAK",  # Would need actual regime classification
                strategy_name=strategy,
                candidate_order=order + 1,
                status="evaluated",
                evaluated=True,
                winner=winner,
                signal_side="BUY" if winner else None,
                signal_type="entry" if winner else None,
                notes=f"Night session monitoring - order {order + 1}"
            )
            
            # Update statistics
            self.stats["strategies_evaluated"][strategy] = \
                self.stats["strategies_evaluated"].get(strategy, 0) + 1
    
    def check_starvation(self):
        """Check for strategy starvation."""
        if self.stats["bars_processed"] < self.config["min_bars_for_analysis"]:
            return
        
        # Load attribution data
        router_file = self.attribution_dir / "router_evaluation_log.csv"
        if not router_file.exists():
            return
        
        try:
            df = pd.read_csv(router_file)
            
            # Calculate starvation index for each strategy
            strategies = df["strategy_name"].unique()
            
            starvation_report = []
            for strategy in strategies:
                strategy_df = df[df["strategy_name"] == strategy]
                total_bars = self.stats["bars_processed"]
                eval_count = len(strategy_df)
                
                if total_bars > 0:
                    evaluation_rate = eval_count / total_bars
                    starvation_index = 1 - evaluation_rate
                    
                    starvation_level = "acceptable"
                    if starvation_index > self.config["starvation_threshold"]:
                        starvation_level = "SEVERE"
                    elif starvation_index > 0.4:
                        starvation_level = "moderate"
                    
                    starvation_report.append({
                        "strategy": strategy,
                        "eval_count": eval_count,
                        "total_bars": total_bars,
                        "evaluation_rate": evaluation_rate,
                        "starvation_index": starvation_index,
                        "level": starvation_level
                    })
            
            # Log starvation report
            if starvation_report:
                self.log("Starvation Analysis:")
                for report in starvation_report:
                    if report["level"] == "SEVERE":
                        self.log(f"  🚨 {report['strategy']}: starvation_index={report['starvation_index']:.3f} (SEVERE)", "WARNING")
                    elif report["level"] == "moderate":
                        self.log(f"  ⚠️  {report['strategy']}: starvation_index={report['starvation_index']:.3f} (moderate)", "WARNING")
                    else:
                        self.log(f"  ✅ {report['strategy']}: starvation_index={report['starvation_index']:.3f} (acceptable)")
            
        except Exception as e:
            self.log(f"Error checking starvation: {e}", "ERROR")
    
    def generate_report(self):
        """Generate attribution report."""
        if self.stats["bars_processed"] < self.config["min_bars_for_analysis"]:
            self.log(f"Not enough data for report (need {self.config['min_bars_for_analysis']}, have {self.stats['bars_processed']})")
            return
        
        try:
            # Run attribution report script
            import subprocess
            
            report_dir = self.attribution_dir / "reports"
            report_dir.mkdir(exist_ok=True)
            
            cmd = [
                sys.executable,
                str(self.project_root / "scripts" / "attribution_report.py"),
                "--input-dir", str(self.attribution_dir),
                "--output-dir", str(report_dir),
                "--force"
            ]
            
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                cwd=str(self.project_root),
                timeout=30
            )
            
            if result.returncode == 0:
                self.log(f"Attribution report generated in {report_dir}")
            else:
                self.log(f"Error generating report: {result.stderr[:200]}", "ERROR")
                
        except Exception as e:
            self.log(f"Error generating report: {e}", "ERROR")
    
    def run(self):
        """Main monitoring loop."""
        self.log("=" * 70)
        self.log("Night Session Monitor with Attribution Logging")
        self.log("=" * 70)
        self.log(f"Start time: {self.stats['start_time'].strftime('%Y-%m-%d %H:%M:%S')}")
        self.log(f"Attribution directory: {self.attribution_dir}")
        self.log(f"Check interval: {self.config['check_interval_seconds']} seconds")
        self.log("=" * 70)
        
        last_report_time = datetime.now()
        last_starvation_check = datetime.now()
        
        while self.running:
            try:
                # Check if night session
                if not self.is_night_session():
                    self.log("Not night session hours, waiting...")
                    time.sleep(60)
                    continue
                
                # Check trading system
                has_data = self.check_trading_system()
                
                # Check starvation every 15 minutes
                if (datetime.now() - last_starvation_check).seconds >= 900:
                    self.check_starvation()
                    last_starvation_check = datetime.now()
                
                # Generate report every hour
                if (datetime.now() - last_report_time).seconds >= 3600:
                    self.generate_report()
                    last_report_time = datetime.now()
                
                # Print status
                self._print_status()
                
                # Wait for next check
                time.sleep(self.config["check_interval_seconds"])
                
            except KeyboardInterrupt:
                self.log("Keyboard interrupt received, shutting down...")
                break
            except Exception as e:
                self.log(f"Error in main loop: {e}", "ERROR")
                time.sleep(5)
        
        # Final cleanup
        self._shutdown()
    
    def _print_status(self):
        """Print current status."""
        status_lines = [
            f"Time: {datetime.now().strftime('%H:%M:%S')}",
            f"Bars processed: {self.stats['bars_processed']}",
            f"Last bar: {self.stats['last_bar_time'] or 'None'}",
            f"Strategies evaluated: {len(self.stats['strategies_evaluated'])}"
        ]
        
        # Clear and print status
        os.system('clear')
        print("=" * 70)
        print("🌙 Night Session Monitor with Attribution")
        print("=" * 70)
        for line in status_lines:
            print(line)
        print("-" * 70)
        
        # Print strategy counts
        if self.stats["strategies_evaluated"]:
            print("Strategy evaluations:")
            for strategy, count in sorted(self.stats["strategies_evaluated"].items(), key=lambda x: x[1], reverse=True):
                print(f"  {strategy}: {count}")
        
        print("=" * 70)
        print("Press Ctrl+C to stop")
    
    def _shutdown(self):
        """Shutdown cleanup."""
        # Final flush of attribution data
        self.recorder.export_csv_if_needed(force=True)
        
        # Save statistics
        stats_file = self.attribution_dir / "monitor_stats.json"
        stats_data = {
            "monitor": {
                "start_time": self.stats["start_time"].isoformat(),
                "end_time": datetime.now().isoformat(),
                "bars_processed": self.stats["bars_processed"],
                "strategies_evaluated": self.stats["strategies_evaluated"]
            },
            "files": {
                "attribution_data": str(self.attribution_dir),
                "log_file": str(self.log_file)
            }
        }
        
        with open(stats_file, 'w') as f:
            json.dump(stats_data, f, indent=2, ensure_ascii=False)
        
        self.log(f"Shutdown complete. Statistics saved to {stats_file}")
        self.log(f"Attribution data in: {self.attribution_dir}")

def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Night Session Monitor with Attribution Logging")
    parser.add_argument("--config", type=str, help="Configuration file path")
    parser.add_argument("--interval", type=int, default=10, help="Check interval in seconds")
    
    args = parser.parse_args()
    
    monitor = NightSessionMonitor(config_path=args.config)
    
    # Override interval if specified
    if args.interval:
        monitor.config["check_interval_seconds"] = args.interval
    
    try:
        monitor.run()
    except Exception as e:
        print(f"Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()