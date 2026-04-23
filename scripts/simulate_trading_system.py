#!/usr/bin/env python3
"""
Simulated Trading System for Attribution Testing

This script simulates a trading system that generates bar data and logs it
to unified.log, allowing the attribution monitor to collect data.
"""

import sys
import os
import time
import json
import random
from pathlib import Path
from datetime import datetime, timedelta

# Add project root to path
PROJECT_ROOT = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
sys.path.insert(0, str(PROJECT_ROOT))

class SimulatedTradingSystem:
    """Simulate a trading system for attribution testing."""
    
    def __init__(self):
        self.project_root = PROJECT_ROOT
        
        # Log file
        self.log_file = self.project_root / "logs" / "unified.log"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Create log file if it doesn't exist
        if not self.log_file.exists():
            with open(self.log_file, 'w') as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [INFO] Log file created\n")
        
        # Simulation parameters
        self.base_price = 20000
        self.bar_interval = 5  # 5-minute bars
        self.symbol = "TX"
        
        # Statistics
        self.stats = {
            "bars_generated": 0,
            "start_time": datetime.now(),
            "last_bar_time": None
        }
    
    def log(self, message, level="INFO"):
        """Log message to unified.log."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_message = f"[{timestamp}] [{level}] {message}"
        
        with open(self.log_file, 'a') as f:
            f.write(log_message + "\n")
        
        print(log_message)
    
    def generate_bar(self):
        """Generate a simulated bar."""
        # Simulate price movement
        price_change = random.randint(-20, 20)
        self.base_price += price_change
        
        # Ensure price stays within reasonable range
        self.base_price = max(19000, min(21000, self.base_price))
        
        # Create bar data
        bar = {
            "timestamp": datetime.now(),
            "symbol": self.symbol,
            "open": self.base_price - random.randint(5, 15),
            "high": self.base_price + random.randint(10, 30),
            "low": self.base_price - random.randint(10, 30),
            "close": self.base_price,
            "volume": random.randint(800, 1500)
        }
        
        return bar
    
    def simulate_router_decision(self, bar):
        """Simulate router decision for attribution."""
        strategies = ["counter_vwap", "spring_upthrust", "kbar_feature"]
        
        # Randomly select winner
        winner_index = random.randint(0, len(strategies) - 1)
        
        decision = {
            "timestamp": bar["timestamp"],
            "symbol": bar["symbol"],
            "strategies": strategies,
            "winner": strategies[winner_index],
            "candidates": strategies,
            "regime": random.choice(["WEAK", "STRONG", "NEUTRAL"])
        }
        
        return decision
    
    def run_simulation(self, duration_minutes=60, bar_interval=5):
        """Run simulation for specified duration."""
        self.log("=" * 70)
        self.log("Simulated Trading System for Attribution Testing")
        self.log("=" * 70)
        self.log(f"Duration: {duration_minutes} minutes")
        self.log(f"Bar interval: {bar_interval} minutes")
        self.log(f"Log file: {self.log_file}")
        self.log("=" * 70)
        
        total_bars = duration_minutes // bar_interval
        
        for i in range(total_bars):
            # Generate bar
            bar = self.generate_bar()
            
            # Log bar
            self.log(f"[FuturesMonitor] New Bar: {bar['symbol']} {bar_interval}m "
                    f"open={bar['open']:.1f} high={bar['high']:.1f} "
                    f"low={bar['low']:.1f} close={bar['close']:.1f} "
                    f"volume={bar['volume']}")
            
            # Simulate router decision
            decision = self.simulate_router_decision(bar)
            
            # Log decision (this would trigger attribution in real system)
            self.log(f"[Router] Evaluation: candidates={','.join(decision['candidates'])} "
                    f"winner={decision['winner']} regime={decision['regime']}")
            
            # Occasionally log MTX price updates
            if i % 3 == 0:
                self.log(f"✅ MTX updated: {bar['close']:.1f}")
            
            # Update statistics
            self.stats["bars_generated"] += 1
            self.stats["last_bar_time"] = bar["timestamp"]
            
            # Print progress
            progress = (i + 1) / total_bars * 100
            print(f"\rProgress: {progress:.1f}% | Bars: {i + 1}/{total_bars}", end="")
            
            # Wait for next bar
            if i < total_bars - 1:
                time.sleep(bar_interval * 60 / 10)  # Speed up for testing
        
        # Final summary
        self.log("\n" + "=" * 70)
        self.log("Simulation Complete")
        self.log("=" * 70)
        self.log(f"Total bars generated: {self.stats['bars_generated']}")
        self.log(f"Duration: {duration_minutes} minutes")
        self.log(f"Log file size: {self.log_file.stat().st_size / 1024:.1f} KB")
        self.log("=" * 70)
        
        # Save simulation stats
        stats_file = self.project_root / "logs" / "simulation_stats.json"
        stats_data = {
            "simulation": {
                "start_time": self.stats["start_time"].isoformat(),
                "end_time": datetime.now().isoformat(),
                "duration_minutes": duration_minutes,
                "bars_generated": self.stats["bars_generated"],
                "bar_interval": bar_interval
            },
            "log_file": str(self.log_file)
        }
        
        with open(stats_file, 'w') as f:
            json.dump(stats_data, f, indent=2, ensure_ascii=False)
        
        self.log(f"Statistics saved to: {stats_file}")

def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Simulated Trading System for Attribution Testing")
    parser.add_argument("--duration", type=int, default=60, help="Simulation duration in minutes")
    parser.add_argument("--interval", type=int, default=5, help="Bar interval in minutes")
    parser.add_argument("--real-time", action="store_true", help="Run in real-time (1 bar per interval)")
    
    args = parser.parse_args()
    
    system = SimulatedTradingSystem()
    
    if args.real_time:
        print("Running in real-time mode (1 bar per interval)")
        print(f"Each bar will take {args.interval} minutes")
        print("Press Ctrl+C to stop")
        
        try:
            while True:
                bar = system.generate_bar()
                system.log(f"[FuturesMonitor] New Bar: {bar['symbol']} {args.interval}m "
                          f"close={bar['close']:.1f}")
                
                # Simulate router decision
                decision = system.simulate_router_decision(bar)
                system.log(f"[Router] Evaluation: winner={decision['winner']}")
                
                # Update price
                if random.random() < 0.3:
                    system.log(f"✅ MTX updated: {bar['close']:.1f}")
                
                system.stats["bars_generated"] += 1
                
                # Wait for next bar
                time.sleep(args.interval * 60)
                
        except KeyboardInterrupt:
            print("\nSimulation stopped by user")
            
    else:
        # Fast simulation for testing
        print(f"Running fast simulation ({args.duration} minutes in accelerated time)")
        system.run_simulation(
            duration_minutes=args.duration,
            bar_interval=args.interval
        )

if __name__ == "__main__":
    main()