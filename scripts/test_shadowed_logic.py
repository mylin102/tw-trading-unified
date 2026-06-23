#!/usr/bin/env python3
"""
Test router shadowed logic directly.
"""

import sys
import os
from pathlib import Path
import random

# Add project root to path
PROJECT_ROOT = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
sys.path.insert(0, str(PROJECT_ROOT))

def test_router_shadowed_logic():
    """Test if router correctly logs shadowed strategies."""
    
    print("=" * 70)
    print("Router Shadowed Logic Test")
    print("=" * 70)
    
    # Import necessary modules
    try:
        from core.attribution_recorder import AttributionRecorder
        from core.futures_strategy_router import route_futures_signal
        
        # Create attribution recorder
        test_dir = PROJECT_ROOT / "data" / "attribution" / "shadowed_test"
        test_dir.mkdir(parents=True, exist_ok=True)
        
        recorder = AttributionRecorder(
            output_dir=test_dir,
            buffer_size=10,  # Small buffer for testing
            flush_interval_seconds=5,
            flush_on_exit=True
        )
        
        # Create mock registry
        class MockStrategy:
            def __init__(self, name, win_probability):
                self.name = name
                self.win_probability = win_probability
                
            def on_bar(self, context):
                # Simulate strategy decision
                if random.random() < self.win_probability:
                    # Return a mock signal
                    class MockSignal:
                        def __init__(self):
                            self.action = "BUY" if random.random() < 0.5 else "SELL"
                            self.type = "entry"
                            self.score = random.randint(50, 100)
                            
                        def validate(self):
                            return True, ""
                            
                    return MockSignal()
                return None
        
        # Create strategies with different win probabilities
        strategies = {
            "counter_vwap": MockStrategy("counter_vwap", 0.6),  # 60% win rate
            "spring_upthrust": MockStrategy("spring_upthrust", 0.3),  # 30% win rate
            "kbar_feature": MockStrategy("kbar_feature", 0.1),  # 10% win rate
        }
        
        class MockRegistry:
            def __init__(self, strategies):
                self.strategies = strategies
                
            def get(self, name):
                return self.strategies.get(name)
        
        registry = MockRegistry(strategies)
        
        # Create mock context
        class MockMarketData:
            def __init__(self):
                self.timestamp = "2026-04-23 01:00:00"
                self.last_bar = {
                    "timestamp": "2026-04-23 01:00:00",
                    "symbol": "TX",
                    "open": 20000,
                    "high": 20050,
                    "low": 19950,
                    "close": 20025,
                    "volume": 1000
                }
                self.symbol = "TX"
                self.contract = "TX"
        
        class MockPosition:
            def __init__(self):
                self.size = 0
                self.entry_price = 0
                self.current_stop_loss = None
                self.unrealized_pnl = 0
        
        class MockStrategyContext:
            def __init__(self):
                self.market = MockMarketData()
                self.position = MockPosition()
                self.config = {}
                self.bar_counter = 0
        
        context = MockStrategyContext()
        
        # Create mock regime result
        class MockRegimeResult:
            def __init__(self):
                self.regime = "WEAK"
                self.bias = "NEUTRAL"
        
        regime_result = MockRegimeResult()
        
        print("\nRunning router with attribution recorder...")
        print("Strategies in order: counter_vwap, spring_upthrust, kbar_feature")
        print("Win probabilities: 60%, 30%, 10%")
        print()
        
        # Run router multiple times
        results = []
        for i in range(10):
            print(f"Run {i+1}: ", end="")
            
            # Call router with attribution recorder
            decision = route_futures_signal(
                registry=registry,
                context=context,
                regime_result=regime_result,
                active_strategy_name=None,  # Auto-select
                current_working_orders=None,
                is_flattening=False,
                router_config=None,
                prepare_strategy=None,
                recorder=recorder  # Pass the recorder!
            )
            
            # Get winner
            winner = decision.action if decision else "None"
            print(f"Winner: {winner}")
            
            # Update market data timestamp for next run
            context.market.timestamp = f"2026-04-23 01:{i+1:02d}:00"
            context.market.last_bar["timestamp"] = context.market.timestamp
        
        # Force flush to get CSV
        recorder.export_csv_if_needed(force=True)
        
        print("\n" + "=" * 70)
        print("Analyzing Attribution Data")
        print("=" * 70)
        
        # Read the generated CSV
        import pandas as pd
        csv_file = test_dir / "router_evaluation_log.csv"
        
        if csv_file.exists():
            df = pd.read_csv(csv_file)
            print(f"Total rows: {len(df)}")
            print(f"Unique timestamps: {df['timestamp'].nunique()}")
            
            # Analyze by strategy
            strategies = df["strategy_name"].unique()
            
            print("\nStrategy Analysis:")
            print("-" * 70)
            
            for strategy in strategies:
                if strategy == "router":  # Skip router meta entries
                    continue
                    
                strategy_df = df[df["strategy_name"] == strategy]
                
                # Count different statuses
                total = len(strategy_df)
                evaluated = len(strategy_df[strategy_df["evaluated"] == True])
                shadowed = len(strategy_df[strategy_df["status"] == "shadowed"])
                winner = len(strategy_df[strategy_df["winner"] == True])
                no_signal = len(strategy_df[strategy_df["status"] == "no_signal"])
                
                print(f"\n{strategy}:")
                print(f"  Total entries: {total}")
                print(f"  Evaluated: {evaluated} ({evaluated/total*100:.1f}%)")
                print(f"  Shadowed: {shadowed} ({shadowed/total*100:.1f}%)")
                print(f"  Winner: {winner} ({winner/total*100:.1f}%)")
                print(f"  No signal: {no_signal} ({no_signal/total*100:.1f}%)")
                
                # Check for correct shadowed logic
                if shadowed > 0:
                    print(f"  ✅ CORRECT: Has shadowed entries")
                else:
                    print(f"  ⚠️  WARNING: No shadowed entries (possible bug)")
            
            # Check short-circuit logic
            print("\n" + "=" * 70)
            print("Short-Circuit Logic Verification")
            print("=" * 70)
            
            # Group by timestamp
            timestamps = df["timestamp"].unique()
            
            for ts in timestamps[:5]:  # Check first 5 timestamps
                ts_df = df[df["timestamp"] == ts]
                
                # Find winner in this timestamp
                winner_row = ts_df[ts_df["winner"] == True]
                
                if len(winner_row) > 0:
                    winner_strategy = winner_row.iloc[0]["strategy_name"]
                    winner_order = winner_row.iloc[0]["candidate_order"]
                    
                    # Check if strategies after winner are shadowed
                    after_winner = ts_df[ts_df["candidate_order"] > winner_order]
                    shadowed_after = after_winner[after_winner["status"] == "shadowed"]
                    
                    print(f"\nTimestamp: {ts}")
                    print(f"  Winner: {winner_strategy} (order {winner_order})")
                    print(f"  Strategies after winner: {len(after_winner)}")
                    print(f"  Shadowed after winner: {len(shadowed_after)}")
                    
                    if len(after_winner) == len(shadowed_after):
                        print(f"  ✅ CORRECT: All strategies after winner are shadowed")
                    else:
                        print(f"  ❌ ERROR: Not all strategies after winner are shadowed")
                        print(f"     Expected: {len(after_winner)} shadowed, Got: {len(shadowed_after)}")
                else:
                    print(f"\nTimestamp: {ts}")
                    print(f"  No winner - all strategies should be evaluated")
                    print(f"  Total strategies: {len(ts_df)}")
                    print(f"  Evaluated: {len(ts_df[ts_df['evaluated'] == True])}")
                    print(f"  Shadowed: {len(ts_df[ts_df['status'] == 'shadowed'])}")
            
            # Calculate starvation metrics
            print("\n" + "=" * 70)
            print("Starvation Analysis (CORRECT)")
            print("=" * 70)
            
            total_bars = df["timestamp"].nunique()
            
            for strategy in strategies:
                if strategy == "router":
                    continue
                    
                strategy_df = df[df["strategy_name"] == strategy]
                
                # CORRECT metrics
                candidate_count = total_bars  # Each bar is a candidate opportunity
                eval_count = len(strategy_df[strategy_df["evaluated"] == True])
                shadow_count = len(strategy_df[strategy_df["status"] == "shadowed"])
                
                if candidate_count > 0:
                    evaluation_rate = eval_count / candidate_count
                    shadow_rate = shadow_count / candidate_count
                    starvation_index = 1 - evaluation_rate
                    
                    print(f"\n{strategy}:")
                    print(f"  Candidate bars: {candidate_count}")
                    print(f"  Evaluated: {eval_count} ({evaluation_rate:.1%})")
                    print(f"  Shadowed: {shadow_count} ({shadow_rate:.1%})")
                    print(f"  Starvation index: {starvation_index:.3f}")
                    
                    if starvation_index > 0.7:
                        print(f"  🚨 SEVERE starvation")
                    elif starvation_index > 0.4:
                        print(f"  ⚠️  Moderate starvation")
                    else:
                        print(f"  ✅ Acceptable")
        
        else:
            print("❌ No CSV file generated")
            
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_router_shadowed_logic()