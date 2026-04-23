#!/usr/bin/env python3
"""
Attribution report generator for futures strategy router.

Usage:
    python scripts/attribution_report.py --input-dir ./data/attribution --output-dir ./reports
    python scripts/attribution_report.py --input-dir ./data/attribution --strategy kbar_feature
    python scripts/attribution_report.py --input-dir ./data/attribution --regime WEAK
    python scripts/attribution_report.py --input-dir ./data/attribution --summary-only

Generates:
    1. router_summary.csv - Strategy exposure and starvation stats
    2. regime_summary.csv - Performance by regime
    3. starvation_report.csv - Focus on shadowed strategies
    4. priority_impact_report.csv - Strategies suppressed by priority
    5. trade_performance.csv - PnL and metrics by strategy
    6. merged_summary.csv - Combined router + trade stats
    7. visualizations/ - Charts (if matplotlib available)
"""

import argparse
import sys
import os
from pathlib import Path
import pandas as pd
from typing import Optional, Tuple, List
import json
from datetime import datetime

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.attribution_recorder import (
    summarize_router,
    summarize_router_by_regime,
    summarize_signals,
    summarize_trades,
    merge_router_and_trade_summary,
    build_starvation_report,
)


def load_attribution_data(input_dir: Path) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Load router, signal, and trade CSV files."""
    router_path = input_dir / "router_evaluation_log.csv"
    signal_path = input_dir / "strategy_signal_log.csv"
    trade_path = input_dir / "trade_attribution_log.csv"
    
    router_df = pd.read_csv(router_path) if router_path.exists() else pd.DataFrame()
    signal_df = pd.read_csv(signal_path) if signal_path.exists() else pd.DataFrame()
    trade_df = pd.read_csv(trade_path) if trade_path.exists() else pd.DataFrame()
    
    print(f"Loaded data from {input_dir}:")
    print(f"  Router rows: {len(router_df):,}")
    print(f"  Signal rows: {len(signal_df):,}")
    print(f"  Trade rows: {len(trade_df):,}")
    
    return router_df, signal_df, trade_df


def generate_router_summary(router_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """Generate router exposure summary."""
    if router_df.empty:
        print("Warning: No router data available")
        return pd.DataFrame()
    
    summary = summarize_router(router_df)
    if not summary.empty:
        output_path = output_dir / "router_summary.csv"
        summary.to_csv(output_path, index=False)
        print(f"Router summary saved to {output_path}")
        
        # Print top strategies
        print("\nTop strategies by candidate count:")
        top_candidates = summary.nlargest(5, "candidate_count")[["strategy_name", "candidate_count", "eval_count", "winner_count"]]
        print(top_candidates.to_string(index=False))
        
        # Print starvation concerns
        starvation = summary[summary["starvation_index"] > 0.3]
        if not starvation.empty:
            print("\n⚠️  Starvation concerns (starvation_index > 0.3):")
            print(starvation[["strategy_name", "starvation_index", "shadowed_count", "eval_count"]].to_string(index=False))
    
    return summary


def generate_regime_summary(router_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """Generate summary by strategy x regime."""
    if router_df.empty:
        return pd.DataFrame()
    
    regime_summary = summarize_router_by_regime(router_df)
    if not regime_summary.empty:
        output_path = output_dir / "regime_summary.csv"
        regime_summary.to_csv(output_path, index=False)
        print(f"Regime summary saved to {output_path}")
        
        # Print regime distribution
        print("\nRegime distribution:")
        regime_counts = router_df["regime"].value_counts()
        for regime, count in regime_counts.items():
            print(f"  {regime}: {count:,} bars")
    
    return regime_summary


def generate_starvation_report(router_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """Generate focused starvation report."""
    if router_df.empty:
        return pd.DataFrame()
    
    starvation = build_starvation_report(router_df)
    if not starvation.empty:
        output_path = output_dir / "starvation_report.csv"
        starvation.to_csv(output_path, index=False)
        print(f"Starvation report saved to {output_path}")
        
        # Highlight severe starvation
        severe = starvation[starvation["starvation_level"] == "severe"]
        if not severe.empty:
            print("\n🔴 SEVERE STARVATION (index > 0.70):")
            print(severe[["strategy_name", "starvation_index", "shadowed_count", "eval_count"]].to_string(index=False))
        
        moderate = starvation[starvation["starvation_level"] == "moderate"]
        if not moderate.empty:
            print("\n🟡 MODERATE STARVATION (0.40-0.70):")
            print(moderate[["strategy_name", "starvation_index", "shadowed_count", "eval_count"]].to_string(index=False))
    
    return starvation


def generate_priority_impact_report(router_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """Generate priority impact report (strategies suppressed by priority)."""
    if router_df.empty:
        return pd.DataFrame()
    
    summary = summarize_router(router_df)
    if summary.empty:
        return pd.DataFrame()
    
    # Filter strategies with shadowed_count > 0
    shadowed = summary[summary["shadowed_count"] > 0].copy()
    if shadowed.empty:
        print("No strategies were shadowed (priority impact = 0 for all)")
        return pd.DataFrame()
    
    # Sort by priority_impact (higher = more suppressed)
    shadowed = shadowed.sort_values("priority_impact", ascending=False)
    
    output_path = output_dir / "priority_impact_report.csv"
    shadowed.to_csv(output_path, index=False)
    print(f"Priority impact report saved to {output_path}")
    
    # Print priority impact analysis
    print("\nPriority Impact Analysis (shadowed_count / winner_count):")
    print("Higher = more suppressed by higher-priority strategies")
    for _, row in shadowed.head(10).iterrows():
        impact = row["priority_impact"]
        level = "🔴 HIGH" if impact > 3.0 else "🟡 MEDIUM" if impact > 1.0 else "🟢 LOW"
        print(f"  {row['strategy_name']}: {impact:.2f} ({level}) - "
              f"shadowed {row['shadowed_count']:,}x, won {row['winner_count']:,}x")
    
    return shadowed


def generate_trade_performance(trade_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """Generate trade performance summary."""
    if trade_df.empty:
        print("Warning: No trade data available")
        return pd.DataFrame()
    
    trade_summary = summarize_trades(trade_df)
    if not trade_summary.empty:
        output_path = output_dir / "trade_performance.csv"
        trade_summary.to_csv(output_path, index=False)
        print(f"Trade performance saved to {output_path}")
        
        # Print performance highlights
        print("\nTrade Performance Highlights:")
        
        # Best by total PnL
        best_pnl = trade_summary.nlargest(3, "total_pnl")
        if not best_pnl.empty:
            print("Top strategies by total PnL:")
            for _, row in best_pnl.iterrows():
                print(f"  {row['strategy_name']}: ${row['total_pnl']:.2f} "
                      f"(win rate: {row['win_rate']:.1%}, trades: {row['trade_count']})")
        
        # Best by win rate
        high_winrate = trade_summary[trade_summary["trade_count"] >= 5].nlargest(3, "win_rate")
        if not high_winrate.empty:
            print("\nTop strategies by win rate (min 5 trades):")
            for _, row in high_winrate.iterrows():
                print(f"  {row['strategy_name']}: {row['win_rate']:.1%} "
                      f"(PnL: ${row['total_pnl']:.2f}, trades: {row['trade_count']})")
    
    return trade_summary


def generate_merged_summary(router_df: pd.DataFrame, trade_df: pd.DataFrame, output_dir: Path) -> pd.DataFrame:
    """Generate merged router + trade summary."""
    if router_df.empty and trade_df.empty:
        return pd.DataFrame()
    
    merged = merge_router_and_trade_summary(router_df, trade_df)
    if not merged.empty:
        output_path = output_dir / "merged_summary.csv"
        merged.to_csv(output_path, index=False)
        print(f"Merged summary saved to {output_path}")
        
        # Calculate efficiency metrics
        if "trade_count" in merged.columns and "eval_count" in merged.columns:
            merged["efficiency"] = merged["trade_count"] / merged["eval_count"]
            merged["shadow_cost"] = merged["shadowed_count"] * merged.get("avg_pnl", 0)
        else:
            merged["efficiency"] = 0.0
            merged["shadow_cost"] = 0.0
        
        # Print efficiency analysis
        if "efficiency" in merged.columns and "eval_count" in merged.columns:
            efficient = merged[merged["eval_count"] >= 5].nlargest(5, "efficiency")
            if not efficient.empty:
                # Build column list based on available columns
                columns_to_show = ["strategy_name", "efficiency", "eval_count"]
                if "trade_count" in efficient.columns:
                    columns_to_show.append("trade_count")
                if "win_rate" in efficient.columns:
                    columns_to_show.append("win_rate")
                
                print("\nMost efficient strategies (trades per evaluation):")
                print(efficient[columns_to_show].to_string(index=False))
    
    return merged


def generate_visualizations(router_df: pd.DataFrame, trade_df: pd.DataFrame, output_dir: Path):
    """Generate visualization charts if matplotlib is available."""
    try:
        import matplotlib.pyplot as plt
        import matplotlib
        matplotlib.use('Agg')  # Non-interactive backend
    except ImportError:
        print("Matplotlib not available, skipping visualizations")
        return
    
    vis_dir = output_dir / "visualizations"
    vis_dir.mkdir(exist_ok=True)
    
    # 1. Starvation index bar chart
    if not router_df.empty:
        summary = summarize_router(router_df)
        if not summary.empty and len(summary) > 1:
            plt.figure(figsize=(10, 6))
            bars = plt.barh(summary["strategy_name"], summary["starvation_index"])
            
            # Color by starvation level
            for bar, idx in zip(bars, summary["starvation_index"]):
                if idx > 0.7:
                    bar.set_color('red')
                elif idx > 0.4:
                    bar.set_color('orange')
                else:
                    bar.set_color('green')
            
            plt.xlabel("Starvation Index (1.0 = never evaluated)")
            plt.title("Strategy Starvation Analysis")
            plt.tight_layout()
            plt.savefig(vis_dir / "starvation_index.png", dpi=150)
            plt.close()
    
    # 2. Priority impact scatter plot
    if not router_df.empty:
        summary = summarize_router(router_df)
        shadowed = summary[summary["shadowed_count"] > 0]
        if len(shadowed) >= 3:
            plt.figure(figsize=(10, 6))
            plt.scatter(shadowed["winner_count"], shadowed["shadowed_count"], 
                       s=shadowed["priority_impact"] * 50, alpha=0.6)
            
            for _, row in shadowed.iterrows():
                plt.annotate(row["strategy_name"], 
                           (row["winner_count"], row["shadowed_count"]),
                           fontsize=9, alpha=0.8)
            
            plt.xlabel("Winner Count")
            plt.ylabel("Shadowed Count")
            plt.title("Priority Impact: Shadowed vs Winner Count")
            plt.grid(True, alpha=0.3)
            plt.tight_layout()
            plt.savefig(vis_dir / "priority_impact.png", dpi=150)
            plt.close()
    
    # 3. Regime distribution pie chart
    if not router_df.empty:
        regime_counts = router_df["regime"].value_counts()
        if len(regime_counts) > 1:
            plt.figure(figsize=(8, 8))
            plt.pie(regime_counts.values, labels=regime_counts.index, autopct='%1.1f%%')
            plt.title("Regime Distribution")
            plt.tight_layout()
            plt.savefig(vis_dir / "regime_distribution.png", dpi=150)
            plt.close()
    
    print(f"Visualizations saved to {vis_dir}")


def generate_strategy_detail_report(strategy_name: str, router_df: pd.DataFrame, 
                                   signal_df: pd.DataFrame, trade_df: pd.DataFrame,
                                   output_dir: Path):
    """Generate detailed report for a specific strategy."""
    detail_dir = output_dir / "strategy_details"
    detail_dir.mkdir(exist_ok=True)
    
    # Filter data for this strategy
    router_filtered = router_df[router_df["strategy_name"] == strategy_name].copy()
    signal_filtered = signal_df[signal_df["strategy_name"] == strategy_name].copy()
    trade_filtered = trade_df[trade_df["strategy_name"] == strategy_name].copy()
    
    if router_filtered.empty and signal_filtered.empty and trade_filtered.empty:
        print(f"Warning: No data found for strategy '{strategy_name}'")
        return
    
    # Create detailed report
    report = {
        "strategy_name": strategy_name,
        "generated_at": datetime.now().isoformat(),
        "router_stats": {},
        "signal_stats": {},
        "trade_stats": {},
        "regime_performance": {},
        "time_analysis": {},
    }
    
    # Router stats
    if not router_filtered.empty:
        report["router_stats"] = {
            "total_candidates": len(router_filtered),
            "evaluated_count": router_filtered["evaluated"].sum(),
            "winner_count": router_filtered["winner"].sum(),
            "shadowed_count": (router_filtered["status"] == "shadowed").sum(),
            "no_signal_count": (router_filtered["status"] == "no_signal").sum(),
            "evaluation_rate": router_filtered["evaluated"].sum() / len(router_filtered),
            "win_rate": router_filtered["winner"].sum() / max(1, router_filtered["evaluated"].sum()),
        }
        
        # Regime breakdown
        regime_counts = router_filtered["regime"].value_counts().to_dict()
        report["regime_performance"]["candidate_distribution"] = regime_counts
    
    # Signal stats
    if not signal_filtered.empty:
        report["signal_stats"] = {
            "total_signals": len(signal_filtered),
            "selected_signals": signal_filtered["selected"].sum(),
            "selection_rate": signal_filtered["selected"].sum() / len(signal_filtered),
            "side_distribution": signal_filtered["side"].value_counts().to_dict(),
            "avg_score": signal_filtered["score"].mean() if "score" in signal_filtered.columns else None,
        }
    
    # Trade stats
    if not trade_filtered.empty:
        report["trade_stats"] = {
            "total_trades": len(trade_filtered),
            "total_pnl": trade_filtered["pnl"].sum(),
            "win_count": (trade_filtered["pnl"] > 0).sum(),
            "loss_count": (trade_filtered["pnl"] <= 0).sum(),
            "win_rate": (trade_filtered["pnl"] > 0).sum() / len(trade_filtered),
            "avg_pnl": trade_filtered["pnl"].mean(),
            "avg_mae": trade_filtered["mae"].mean() if "mae" in trade_filtered.columns else None,
            "avg_mfe": trade_filtered["mfe"].mean() if "mfe" in trade_filtered.columns else None,
        }
    
    # Save detailed report
    output_path = detail_dir / f"{strategy_name}_detail.json"
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2, default=str)
    
    # Also save as CSV for easy viewing
    csv_path = detail_dir / f"{strategy_name}_detail.csv"
    
    csv_data = []
    for section, data in report.items():
        if isinstance(data, dict):
            for key, value in data.items():
                csv_data.append({"section": section, "metric": key, "value": value})
    
    if csv_data:
        pd.DataFrame(csv_data).to_csv(csv_path, index=False)
    
    print(f"Detailed report for '{strategy_name}' saved to {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate attribution reports for futures strategy router")
    parser.add_argument("--input-dir", type=Path, default=Path("./data/attribution"),
                       help="Directory containing attribution CSV files")
    parser.add_argument("--output-dir", type=Path, default=Path("./reports/attribution"),
                       help="Directory to save reports")
    parser.add_argument("--strategy", type=str, 
                       help="Generate detailed report for specific strategy")
    parser.add_argument("--regime", type=str,
                       help="Filter analysis to specific regime")
    parser.add_argument("--summary-only", action="store_true",
                       help="Only generate summary reports, skip visualizations")
    parser.add_argument("--force", action="store_true",
                       help="Overwrite existing reports")
    
    args = parser.parse_args()
    
    # Create output directory
    args.output_dir.mkdir(parents=True, exist_ok=True)
    
    # Load data
    router_df, signal_df, trade_df = load_attribution_data(args.input_dir)
    
    if router_df.empty and signal_df.empty and trade_df.empty:
        print(f"Error: No attribution data found in {args.input_dir}")
        print("Expected files: router_evaluation_log.csv, strategy_signal_log.csv, trade_attribution_log.csv")
        sys.exit(1)
    
    # Apply filters if specified
    if args.regime and not router_df.empty:
        router_df = router_df[router_df["regime"] == args.regime].copy()
        print(f"Filtered to regime: {args.regime} ({len(router_df)} rows)")
    
    # Generate reports
    print("\n" + "="*60)
    print("Generating Attribution Reports")
    print("="*60)
    
    router_summary = generate_router_summary(router_df, args.output_dir)
    regime_summary = generate_regime_summary(router_df, args.output_dir)
    starvation_report = generate_starvation_report(router_df, args.output_dir)
    priority_report = generate_priority_impact_report(router_df, args.output_dir)
    trade_performance = generate_trade_performance(trade_df, args.output_dir)
    merged_summary = generate_merged_summary(router_df, trade_df, args.output_dir)
    
    # Generate visualizations
    if not args.summary_only:
        generate_visualizations(router_df, trade_df, args.output_dir)
    
    # Generate strategy detail report if requested
    if args.strategy:
        generate_strategy_detail_report(args.strategy, router_df, signal_df, trade_df, args.output_dir)
    
    # Generate summary markdown report
    generate_summary_markdown(router_summary, trade_performance, starvation_report, args.output_dir)
    
    print("\n" + "="*60)
    print("Report Generation Complete")
    print("="*60)
    print(f"Reports saved to: {args.output_dir.absolute()}")
    
    if not router_df.empty:
        total_bars = router_df["timestamp"].nunique()
        print(f"Total bars analyzed: {total_bars:,}")
    
    if not trade_df.empty:
        total_pnl = trade_df["pnl"].sum()
        print(f"Total PnL across all strategies: ${total_pnl:.2f}")


def generate_summary_markdown(router_summary: pd.DataFrame, trade_performance: pd.DataFrame,
                            starvation_report: pd.DataFrame, output_dir: Path):
    """Generate a summary markdown report."""
    if router_summary.empty and trade_performance.empty:
        return
    
    md_path = output_dir / "SUMMARY.md"
    
    with open(md_path, 'w') as f:
        f.write("# Attribution Analysis Summary\n\n")
        f.write(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        # Router Summary
        f.write("## Router Exposure Summary\n\n")
        if not router_summary.empty:
            f.write("| Strategy | Candidate Count | Evaluated | Winner | Shadowed | Starvation Index |\n")
            f.write("|----------|----------------|-----------|--------|----------|------------------|\n")
            for _, row in router_summary.iterrows():
                starvation_emoji = "🔴" if row["starvation_index"] > 0.7 else "🟡" if row["starvation_index"] > 0.4 else "🟢"
                f.write(f"| {row['strategy_name']} | {row['candidate_count']:,} | {row['eval_count']:,} | {row['winner_count']:,} | {row['shadowed_count']:,} | {starvation_emoji} {row['starvation_index']:.2f} |\n")
        else:
            f.write("No router data available.\n")
        
        f.write("\n")
        
        # Trade Performance
        f.write("## Trade Performance Summary\n\n")
        if not trade_performance.empty:
            f.write("| Strategy | Trades | Win Rate | Total PnL | Avg PnL | Profit Factor |\n")
            f.write("|----------|--------|----------|-----------|---------|---------------|\n")
            for _, row in trade_performance.iterrows():
                win_rate_pct = f"{row['win_rate']:.1%}" if pd.notna(row['win_rate']) else "N/A"
                profit_factor = f"{row['profit_factor']:.2f}" if pd.notna(row['profit_factor']) else "N/A"
                f.write(f"| {row['strategy_name']} | {row['trade_count']} | {win_rate_pct} | ${row['total_pnl']:.2f} | ${row['avg_pnl']:.2f} | {profit_factor} |\n")
        else:
            f.write("No trade data available.\n")
        
        f.write("\n")
        
        # Starvation Analysis
        f.write("## Starvation Analysis\n\n")
        if not starvation_report.empty:
            f.write("Strategies with high starvation index may need priority adjustment:\n\n")
            f.write("| Strategy | Starvation Index | Level | Shadowed Count | Evaluation Count |\n")
            f.write("|----------|------------------|-------|----------------|------------------|\n")
            for _, row in starvation_report.iterrows():
                level_emoji = "🔴" if row["starvation_level"] == "severe" else "🟡" if row["starvation_level"] == "moderate" else "🟢"
                f.write(f"| {row['strategy_name']} | {row['starvation_index']:.3f} | {level_emoji} {row['starvation_level']} | {row['shadowed_count']:,} | {row['eval_count']:,} |\n")
        else:
            f.write("No starvation concerns detected.\n")
        
        f.write("\n")
        
        # Recommendations
        f.write("## Recommendations\n\n")
        f.write("1. **Review high-starvation strategies**: Consider adjusting priority order\n")
        f.write("2. **Analyze shadowed strategies**: Check if they would have been profitable\n")
        f.write("3. **Monitor regime distribution**: Ensure strategies are evaluated in appropriate regimes\n")
        f.write("4. **Validate trade performance**: Compare router exposure with actual PnL\n")
    
    print(f"Summary markdown saved to {md_path}")


if __name__ == "__main__":
    main()