#!/usr/bin/env python3
"""
Generate realistic trade attribution data for testing.
"""

import sys
import os
import random
import pandas as pd
from pathlib import Path
from datetime import datetime, timedelta

# Add project root to path
PROJECT_ROOT = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
sys.path.insert(0, str(PROJECT_ROOT))

def generate_trade_attribution_data():
    """Generate realistic trade attribution data."""
    
    print("=" * 70)
    print("Generating Realistic Trade Attribution Data")
    print("=" * 70)
    
    # Read existing router data
    router_file = PROJECT_ROOT / "data" / "attribution" / "real_data" / "router_evaluation_log.csv"
    
    if not router_file.exists():
        print("❌ No router data found")
        return
    
    df = pd.read_csv(router_file)
    
    # Filter winner rows
    winners = df[df["winner"] == True]
    
    print(f"Total router rows: {len(df)}")
    print(f"Winner rows: {len(winners)}")
    print(f"Unique winners: {winners['strategy_name'].value_counts().to_dict()}")
    
    # Generate trade data based on winners
    trade_data = []
    
    strategies = {
        "counter_vwap": {"win_rate": 0.6, "avg_pnl": 50, "avg_duration": 5},
        "spring_upthrust": {"win_rate": 0.4, "avg_pnl": 30, "avg_duration": 3},
        "kbar_feature": {"win_rate": 0.3, "avg_pnl": 20, "avg_duration": 2}
    }
    
    # Generate trades for each winner
    trade_id = 1
    for idx, row in winners.iterrows():
        strategy = row["strategy_name"]
        if strategy == "router":
            continue
            
        # Determine if this signal resulted in a trade
        if random.random() < 0.7:  # 70% of signals result in trades
            # Generate trade details
            entry_time = datetime.strptime(row["timestamp"], "%Y-%m-%d %H:%M:%S")
            exit_time = entry_time + timedelta(minutes=strategies[strategy]["avg_duration"])
            
            # Determine if trade was profitable
            is_profitable = random.random() < strategies[strategy]["win_rate"]
            pnl = strategies[strategy]["avg_pnl"] * (1 if is_profitable else -1)
            
            trade_data.append({
                "trade_id": trade_id,
                "timestamp": row["timestamp"],
                "symbol": row["symbol"],
                "strategy_name": strategy,
                "entry_price": 20000 + random.randint(-100, 100),
                "exit_price": 20000 + random.randint(-100, 100) + (50 if is_profitable else -50),
                "quantity": 1,
                "side": row.get("signal_side", "BUY"),
                "pnl": pnl,
                "fees": 8,
                "tax": 2,
                "net_pnl": pnl - 10,
                "duration_minutes": strategies[strategy]["avg_duration"],
                "exit_reason": "TP" if is_profitable else "SL",
                "regime": row["regime"],
                "notes": f"Generated from winner signal at {row['timestamp']}"
            })
            
            trade_id += 1
    
    # Create DataFrame
    trades_df = pd.DataFrame(trade_data)
    
    if len(trades_df) == 0:
        print("❌ No trades generated")
        return
    
    # Save trade attribution data
    output_dir = PROJECT_ROOT / "data" / "attribution" / "trade_data"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    trades_file = output_dir / "trade_attribution_log.csv"
    trades_df.to_csv(trades_file, index=False)
    
    print(f"\n✅ Generated {len(trades_df)} trades")
    print(f"Saved to: {trades_file}")
    
    # Generate summary
    generate_trade_summary(trades_df, output_dir)
    
    # Merge with existing router data
    merge_attribution_data(df, trades_df, output_dir)

def generate_trade_summary(trades_df, output_dir):
    """Generate trade performance summary."""
    
    print("\n" + "=" * 70)
    print("Trade Performance Summary")
    print("=" * 70)
    
    # Group by strategy
    summary = []
    
    for strategy in trades_df["strategy_name"].unique():
        strategy_trades = trades_df[trades_df["strategy_name"] == strategy]
        
        total_trades = len(strategy_trades)
        winning_trades = len(strategy_trades[strategy_trades["net_pnl"] > 0])
        losing_trades = total_trades - winning_trades
        
        win_rate = winning_trades / total_trades if total_trades > 0 else 0
        total_pnl = strategy_trades["net_pnl"].sum()
        avg_pnl = strategy_trades["net_pnl"].mean() if total_trades > 0 else 0
        profit_factor = abs(strategy_trades[strategy_trades["net_pnl"] > 0]["net_pnl"].sum()) / \
                       abs(strategy_trades[strategy_trades["net_pnl"] < 0]["net_pnl"].sum()) if losing_trades > 0 else float('inf')
        
        summary.append({
            "strategy": strategy,
            "total_trades": total_trades,
            "winning_trades": winning_trades,
            "losing_trades": losing_trades,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "profit_factor": profit_factor
        })
        
        print(f"\n{strategy}:")
        print(f"  Trades: {total_trades} (Win: {winning_trades}, Loss: {losing_trades})")
        print(f"  Win rate: {win_rate:.1%}")
        print(f"  Total PnL: {total_pnl:.1f}")
        print(f"  Avg PnL: {avg_pnl:.1f}")
        print(f"  Profit factor: {profit_factor:.2f}")
    
    # Save summary
    summary_df = pd.DataFrame(summary)
    summary_file = output_dir / "trade_performance_summary.csv"
    summary_df.to_csv(summary_file, index=False)
    
    print(f"\n📊 Summary saved to: {summary_file}")

def merge_attribution_data(router_df, trades_df, output_dir):
    """Merge router and trade data for comprehensive analysis."""
    
    print("\n" + "=" * 70)
    print("Merged Attribution Analysis")
    print("=" * 70)
    
    # Calculate strategy efficiency
    strategies = ["counter_vwap", "spring_upthrust", "kbar_feature"]
    
    merged_data = []
    
    for strategy in strategies:
        # Router stats
        router_stats = router_df[router_df["strategy_name"] == strategy]
        
        candidate_count = len(router_df["timestamp"].unique())
        eval_count = len(router_stats[router_stats["evaluated"] == True])
        shadowed_count = len(router_stats[router_stats["status"] == "shadowed"])
        winner_count = len(router_stats[router_stats["winner"] == True])
        
        # Trade stats
        trade_stats = trades_df[trades_df["strategy_name"] == strategy]
        trade_count = len(trade_stats)
        
        # Calculate metrics
        eval_rate = eval_count / candidate_count if candidate_count > 0 else 0
        shadow_rate = shadowed_count / candidate_count if candidate_count > 0 else 0
        starvation_index = 1 - eval_rate
        
        win_efficiency = winner_count / eval_count if eval_count > 0 else 0
        trade_efficiency = trade_count / eval_count if eval_count > 0 else 0
        
        # Trade performance
        total_pnl = trade_stats["net_pnl"].sum() if trade_count > 0 else 0
        avg_pnl = trade_stats["net_pnl"].mean() if trade_count > 0 else 0
        
        merged_data.append({
            "strategy": strategy,
            "candidate_bars": candidate_count,
            "eval_count": eval_count,
            "eval_rate": eval_rate,
            "shadowed_count": shadowed_count,
            "shadow_rate": shadow_rate,
            "starvation_index": starvation_index,
            "winner_count": winner_count,
            "win_efficiency": win_efficiency,
            "trade_count": trade_count,
            "trade_efficiency": trade_efficiency,
            "total_pnl": total_pnl,
            "avg_pnl": avg_pnl,
            "priority_impact": shadowed_count / max(winner_count, 1)
        })
    
    # Create merged DataFrame
    merged_df = pd.DataFrame(merged_data)
    
    # Save merged data
    merged_file = output_dir / "merged_attribution_analysis.csv"
    merged_df.to_csv(merged_file, index=False)
    
    print("\nStrategy Performance Matrix:")
    print("-" * 70)
    
    for row in merged_data:
        print(f"\n{row['strategy']}:")
        print(f"  Exposure: eval={row['eval_rate']:.1%}, shadow={row['shadow_rate']:.1%}")
        print(f"  Efficiency: win={row['win_efficiency']:.1%}, trade={row['trade_efficiency']:.1%}")
        print(f"  Performance: PnL={row['total_pnl']:.1f}, avg={row['avg_pnl']:.1f}")
        print(f"  Priority impact: {row['priority_impact']:.1f}")
        
        # Recommendations
        if row["starvation_index"] > 0.7:
            print(f"  🚨 Severe starvation - consider priority boost")
        elif row["starvation_index"] > 0.4:
            print(f"  ⚠️  Moderate starvation - monitor")
        
        if row["win_efficiency"] < 0.1:
            print(f"  ⚠️  Low win efficiency - consider strategy tuning")
        
        if row["trade_efficiency"] < 0.3:
            print(f"  ⚠️  Low trade conversion - check entry conditions")
    
    print(f"\n📊 Merged analysis saved to: {merged_file}")
    
    # Generate optimization recommendations
    generate_optimization_recommendations(merged_df, output_dir)

def generate_optimization_recommendations(merged_df, output_dir):
    """Generate specific optimization recommendations."""
    
    print("\n" + "=" * 70)
    print("Optimization Recommendations")
    print("=" * 70)
    
    recommendations = []
    
    for _, row in merged_df.iterrows():
        strategy = row["strategy"]
        rec = {"strategy": strategy, "actions": []}
        
        # kbar_feature specific recommendations
        if strategy == "kbar_feature":
            if row["starvation_index"] > 0.6:
                rec["actions"].append("🚨 Priority: Move to position 2 (after counter_vwap)")
            
            if row["win_efficiency"] < 0.1:
                rec["actions"].append("🎯 Tuning: Lower score threshold from -20 to -15")
                rec["actions"].append("🎯 Tuning: Reduce ADX requirement from 20 to 18")
                rec["actions"].append("🎯 Tuning: Add volume spike condition")
            
            if row["trade_efficiency"] < 0.3:
                rec["actions"].append("⚡ Entry: Add momentum confirmation filter")
        
        # spring_upthrust recommendations
        elif strategy == "spring_upthrust":
            if row["starvation_index"] > 0.4:
                rec["actions"].append("⚠️  Monitor: Current position 2 is acceptable")
            
            if row["win_efficiency"] < 0.3:
                rec["actions"].append("🎯 Tuning: Adjust spring detection sensitivity")
        
        # counter_vwap recommendations
        elif strategy == "counter_vwap":
            if row["shadow_rate"] < 0.1:
                rec["actions"].append("✅ Anchor: Keep as priority 1 (high win rate)")
            
            if row["trade_efficiency"] > 0.5:
                rec["actions"].append("⚡ Optimization: Consider partial profit taking")
        
        recommendations.append(rec)
    
    # Save recommendations
    import json
    rec_file = output_dir / "optimization_recommendations.json"
    
    with open(rec_file, 'w') as f:
        json.dump(recommendations, f, indent=2, ensure_ascii=False)
    
    print("\nSpecific Recommendations:")
    print("-" * 70)
    
    for rec in recommendations:
        if rec["actions"]:
            print(f"\n{rec['strategy']}:")
            for action in rec["actions"]:
                print(f"  {action}")
    
    print(f"\n📋 Recommendations saved to: {rec_file}")

if __name__ == "__main__":
    generate_trade_attribution_data()