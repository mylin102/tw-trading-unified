#!/usr/bin/env python3
"""
Run comprehensive reorder simulation with trade data.
"""

import sys
import os
import pandas as pd
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
sys.path.insert(0, str(PROJECT_ROOT))

def run_comprehensive_reorder_simulation():
    """Run reorder simulation with trade data."""
    
    print("=" * 70)
    print("Comprehensive Reorder Simulation with Trade Data")
    print("=" * 70)
    
    # Load merged data
    merged_file = PROJECT_ROOT / "data" / "attribution" / "trade_data" / "merged_attribution_analysis.csv"
    
    if not merged_file.exists():
        print("❌ No merged data found")
        return
    
    merged_df = pd.read_csv(merged_file)
    
    print("\nCurrent Strategy Performance:")
    print("-" * 70)
    
    for _, row in merged_df.iterrows():
        print(f"\n{row['strategy']}:")
        print(f"  Exposure: eval={row['eval_rate']:.1%}, shadow={row['shadow_rate']:.1%}")
        print(f"  Efficiency: win={row['win_efficiency']:.1%}, trade={row['trade_efficiency']:.1%}")
        print(f"  PnL: {row['total_pnl']:.1f} (avg: {row['avg_pnl']:.1f})")
        print(f"  Priority impact: {row['priority_impact']:.1f}")
    
    # Define test orders based on recommendations
    test_orders = [
        # Current order
        ["counter_vwap", "spring_upthrust", "kbar_feature"],
        
        # Conservative: move kbar to position 2
        ["counter_vwap", "kbar_feature", "spring_upthrust"],
        
        # Aggressive: kbar first
        ["kbar_feature", "counter_vwap", "spring_upthrust"],
        
        # Alternative: spring first
        ["spring_upthrust", "counter_vwap", "kbar_feature"],
        
        # Balanced: rotate positions
        ["counter_vwap", "kbar_feature", "spring_upthrust"],
        ["kbar_feature", "spring_upthrust", "counter_vwap"],
        ["spring_upthrust", "kbar_feature", "counter_vwap"]
    ]
    
    # Remove duplicates
    unique_orders = []
    seen = set()
    for order in test_orders:
        order_str = ",".join(order)
        if order_str not in seen:
            seen.add(order_str)
            unique_orders.append(order)
    
    print(f"\nTesting {len(unique_orders)} unique orders...")
    
    # Simulate impact (simplified version)
    simulation_results = []
    
    for order in unique_orders:
        # Calculate expected changes based on current data
        result = simulate_order_impact(order, merged_df)
        simulation_results.append(result)
    
    # Create results DataFrame
    results_df = pd.DataFrame(simulation_results)
    
    # Sort by expected PnL delta
    results_df = results_df.sort_values("expected_pnl_delta", ascending=False)
    
    print("\n" + "=" * 70)
    print("Reorder Simulation Results")
    print("=" * 70)
    
    print("\nTop recommendations:")
    print("-" * 70)
    
    for idx, row in results_df.head(5).iterrows():
        print(f"\nOrder: {row['order']}")
        print(f"  Expected PnL delta: {row['expected_pnl_delta']:+.1f}")
        print(f"  Change rate: {row['change_rate']:.1%}")
        print(f"  kbar exposure: {row['kbar_exposure']:.1%}")
        print(f"  spring exposure: {row['spring_exposure']:.1%}")
        
        if row["expected_pnl_delta"] > 0:
            print(f"  ✅ Expected improvement")
        else:
            print(f"  ⚠️  Expected degradation")
    
    # Save results
    output_dir = PROJECT_ROOT / "data" / "attribution" / "reorder_simulation"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    results_file = output_dir / "comprehensive_reorder_results.csv"
    results_df.to_csv(results_file, index=False)
    
    print(f"\n📊 Results saved to: {results_file}")
    
    # Generate specific recommendation
    generate_specific_recommendation(results_df, merged_df, output_dir)

def simulate_order_impact(order, merged_df):
    """Simulate impact of order change."""
    
    # Create strategy dict for easy access
    strategies = {}
    for _, row in merged_df.iterrows():
        strategies[row["strategy"]] = {
            "eval_rate": row["eval_rate"],
            "win_efficiency": row["win_efficiency"],
            "trade_efficiency": row["trade_efficiency"],
            "avg_pnl": row["avg_pnl"],
            "priority_impact": row["priority_impact"]
        }
    
    # Calculate new exposure rates based on order
    # Simplified model: each strategy gets evaluated unless shadowed by winner
    total_bars = 50  # From our data
    
    # Simulate bar-by-bar
    total_pnl = 0
    changed_decisions = 0
    
    for bar in range(total_bars):
        original_winner = None
        simulated_winner = None
        
        # Original order simulation
        for strategy in ["counter_vwap", "spring_upthrust", "kbar_feature"]:
            if random_evaluate(strategy, strategies[strategy]["win_efficiency"]):
                original_winner = strategy
                break
        
        # New order simulation
        for strategy in order:
            if random_evaluate(strategy, strategies[strategy]["win_efficiency"]):
                simulated_winner = strategy
                break
        
        # Check if decision changed
        if original_winner != simulated_winner:
            changed_decisions += 1
        
        # Calculate PnL impact
        if simulated_winner:
            total_pnl += strategies[simulated_winner]["avg_pnl"]
    
    # Calculate metrics
    change_rate = changed_decisions / total_bars
    
    # Estimate PnL delta (simplified)
    # Based on: more exposure for high-avg-pnl strategies = better
    expected_pnl_delta = 0
    
    # Calculate exposure changes
    kbar_pos = order.index("kbar_feature") if "kbar_feature" in order else 3
    spring_pos = order.index("spring_upthrust") if "spring_upthrust" in order else 3
    
    # Higher position = more exposure
    kbar_exposure = max(0, 1 - (kbar_pos * 0.3))
    spring_exposure = max(0, 1 - (spring_pos * 0.3))
    
    # Weight by strategy performance
    kbar_weight = strategies["kbar_feature"]["avg_pnl"] * strategies["kbar_feature"]["trade_efficiency"]
    spring_weight = strategies["spring_upthrust"]["avg_pnl"] * strategies["spring_upthrust"]["trade_efficiency"]
    
    expected_pnl_delta = (kbar_exposure * kbar_weight) + (spring_exposure * spring_weight)
    
    return {
        "order": ",".join(order),
        "change_rate": change_rate,
        "changed_count": changed_decisions,
        "expected_pnl_delta": expected_pnl_delta,
        "kbar_exposure": kbar_exposure,
        "spring_exposure": spring_exposure,
        "kbar_position": kbar_pos + 1,
        "spring_position": spring_pos + 1
    }

def random_evaluate(strategy, win_efficiency):
    """Randomly determine if strategy wins."""
    import random
    return random.random() < win_efficiency

def generate_specific_recommendation(results_df, merged_df, output_dir):
    """Generate specific recommendation based on simulation."""
    
    print("\n" + "=" * 70)
    print("Specific Recommendation")
    print("=" * 70)
    
    # Get best order
    best_order = results_df.iloc[0]
    
    print(f"\n🎯 Recommended order: {best_order['order']}")
    print(f"   Expected PnL delta: {best_order['expected_pnl_delta']:+.1f}")
    print(f"   Decision change rate: {best_order['change_rate']:.1%}")
    
    # Compare with current
    current_order = "counter_vwap,spring_upthrust,kbar_feature"
    current_row = results_df[results_df["order"] == current_order]
    
    if not current_row.empty:
        current = current_row.iloc[0]
        improvement = best_order["expected_pnl_delta"] - current["expected_pnl_delta"]
        
        print(f"\n📈 Compared to current order:")
        print(f"   PnL improvement: {improvement:+.1f}")
        print(f"   Additional decision changes: {best_order['change_rate'] - current['change_rate']:+.1%}")
    
    # Strategy-specific impacts
    strategies = best_order["order"].split(",")
    
    print("\n📊 Strategy impacts in new order:")
    for i, strategy in enumerate(strategies, 1):
        strategy_data = merged_df[merged_df["strategy"] == strategy]
        
        if not strategy_data.empty:
            row = strategy_data.iloc[0]
            exposure_gain = (1 - (i * 0.2)) - row["eval_rate"]
            
            print(f"\n  {strategy} (position {i}):")
            print(f"    Current exposure: {row['eval_rate']:.1%}")
            print(f"    Expected exposure: {1 - (i * 0.2):.1%}")
            print(f"    Exposure gain: {exposure_gain:+.1%}")
            print(f"    Current PnL: {row['total_pnl']:.1f}")
            
            if exposure_gain > 0:
                print(f"    ✅ Expected improvement")
            else:
                print(f"    ⚠️  Reduced exposure")
    
    # Implementation steps
    print("\n" + "=" * 70)
    print("Implementation Steps")
    print("=" * 70)
    
    print("\n1. Update strategy configuration:")
    print(f"   strategy_list: {strategies}")
    
    print("\n2. Monitor for 1-2 trading days:")
    print("   - Check kbar_feature exposure improvement")
    print("   - Verify counter_vwap performance not degraded")
    print("   - Track overall PnL impact")
    
    print("\n3. Consider strategy tuning (parallel):")
    print("   - Lower kbar_feature score threshold")
    print("   - Add volume confirmation")
    print("   - Adjust spring_upthrust sensitivity")
    
    print("\n4. Schedule re-evaluation:")
    print("   - After 200+ bars of new data")
    print("   - Run attribution report again")
    print("   - Adjust if needed")
    
    # Save recommendation
    import json
    
    recommendation = {
        "recommended_order": best_order["order"].split(","),
        "expected_pnl_delta": float(best_order["expected_pnl_delta"]),
        "change_rate": float(best_order["change_rate"]),
        "implementation_steps": [
            f"Update strategy_list to {strategies}",
            "Monitor for 1-2 trading days",
            "Consider parallel strategy tuning",
            "Re-evaluate after 200+ bars"
        ],
        "monitoring_metrics": [
            "kbar_feature evaluation rate",
            "counter_vwap win rate",
            "Overall PnL",
            "Strategy starvation indices"
        ]
    }
    
    rec_file = output_dir / "specific_recommendation.json"
    with open(rec_file, 'w') as f:
        json.dump(recommendation, f, indent=2, ensure_ascii=False)
    
    print(f"\n📋 Recommendation saved to: {rec_file}")

if __name__ == "__main__":
    run_comprehensive_reorder_simulation()