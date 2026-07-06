#!/usr/bin/env python3
"""
Test script for attribution report generation.

Creates sample attribution data and runs the attribution report script.
"""

import os
import sys
import tempfile
import pandas as pd
from pathlib import Path
import shutil

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from scripts.attribution_report import main as generate_reports
from core.attribution_recorder import AttributionRecorder


def create_sample_data(output_dir: Path):
    """Create sample attribution data for testing."""
    recorder = AttributionRecorder(output_dir=output_dir, buffer_size=5)
    
    # Simulate 100 bars of trading
    for i in range(100):
        timestamp = f"2026-04-22 09:{i:02d}:00"
        regime = "WEAK" if i < 40 else "TREND" if i < 70 else "STRETCHED"
        
        # Log router evaluations
        # counter_vwap (priority 0)
        recorder.log_router_row(
            timestamp=timestamp,
            symbol="TX",
            regime=regime,
            strategy_name="counter_vwap",
            candidate_order=0,
            status="winner" if i % 3 == 0 else "no_signal",
            evaluated=True,
            winner=i % 3 == 0,
            notes=f"score={-10 - i%10}" if i % 3 == 0 else "no signal"
        )
        
        # kbar_feature (priority 1, often shadowed)
        recorder.log_router_row(
            timestamp=timestamp,
            symbol="TX",
            regime=regime,
            strategy_name="kbar_feature",
            candidate_order=1,
            status="shadowed" if i % 3 == 0 else "winner" if i % 5 == 0 else "no_signal",
            evaluated=i % 3 != 0,  # only evaluated when counter_vwap fails
            winner=i % 5 == 0,  # wins occasionally
            notes=f"score={-20 + i%5}" if i % 5 == 0 else "shadowed" if i % 3 == 0 else "no signal"
        )
        
        # spring_upthrust (priority 2, rarely evaluated)
        recorder.log_router_row(
            timestamp=timestamp,
            symbol="TX",
            regime=regime,
            strategy_name="spring_upthrust",
            candidate_order=2,
            status="shadowed" if i % 3 == 0 or i % 5 == 0 else "winner" if i % 7 == 0 else "no_signal",
            evaluated=i % 3 != 0 and i % 5 != 0,  # only evaluated when both higher fail
            winner=i % 7 == 0,
            notes=f"score={-15 + i%3}" if i % 7 == 0 else "shadowed" if i % 3 == 0 or i % 5 == 0 else "no signal"
        )
        
        # Log signals for winning strategies
        if i % 3 == 0:  # counter_vwap wins
            recorder.log_signal(
                timestamp=timestamp,
                symbol="TX",
                regime=regime,
                strategy_name="counter_vwap",
                candidate_order=0,
                side="SELL" if i % 2 == 0 else "BUY",
                signal_type="BREAKOUT",
                selected=True,
                score=-10 - i%10,
                notes="primary signal"
            )
        elif i % 5 == 0:  # kbar_feature wins
            recorder.log_signal(
                timestamp=timestamp,
                symbol="TX",
                regime=regime,
                strategy_name="kbar_feature",
                candidate_order=1,
                side="SELL",
                signal_type="MOMENTUM",
                selected=True,
                score=-20 + i%5,
                notes="secondary signal"
            )
        elif i % 7 == 0:  # spring_upthrust wins
            recorder.log_signal(
                timestamp=timestamp,
                symbol="TX",
                regime=regime,
                strategy_name="spring_upthrust",
                candidate_order=2,
                side="BUY",
                signal_type="REVERSAL",
                selected=True,
                score=-15 + i%3,
                notes="tertiary signal"
            )
        
        # Log trades (simulate PnL)
        if i % 10 == 0:  # Every 10th bar, log a trade
            strategy = "counter_vwap" if i % 30 == 0 else "kbar_feature" if i % 50 == 0 else "spring_upthrust"
            pnl = 50.0 if strategy == "counter_vwap" else 30.0 if strategy == "kbar_feature" else 20.0
            pnl = pnl * (1 if i % 20 == 0 else -1)  # Some losses
            
            recorder.log_trade(
                trade_id=f"T{i:03d}",
                symbol="TX",
                strategy_name=strategy,
                regime_at_entry=regime,
                side="SELL" if i % 2 == 0 else "BUY",
                entry_time=timestamp,
                exit_time=f"2026-04-22 09:{i+3:02d}:00",
                entry_price=20000.0 + i*10,
                exit_price=20000.0 + i*10 + (pnl if strategy == "counter_vwap" else pnl/2),
                pnl=pnl,
                exit_reason="target" if pnl > 0 else "stop_loss",
                mae=abs(pnl) * 0.3 if pnl < 0 else 0,
                mfe=abs(pnl) * 0.8 if pnl > 0 else abs(pnl) * 0.2,
            )
    
    # Force final export
    recorder.export_csv_if_needed(force=True)
    
    print(f"Sample data created in {output_dir}")
    
    # Verify files
    router_path = output_dir / "router_evaluation_log.csv"
    signal_path = output_dir / "strategy_signal_log.csv"
    trade_path = output_dir / "trade_attribution_log.csv"
    
    print(f"  Router rows: {len(pd.read_csv(router_path)) if router_path.exists() else 0}")
    print(f"  Signal rows: {len(pd.read_csv(signal_path)) if signal_path.exists() else 0}")
    print(f"  Trade rows: {len(pd.read_csv(trade_path)) if trade_path.exists() else 0}")


def test_attribution_report():
    """Test the attribution report generation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create sample data
        data_dir = tmpdir / "data"
        data_dir.mkdir()
        create_sample_data(data_dir)
        
        # Generate reports
        report_dir = tmpdir / "reports"
        
        # Mock command line arguments
        import argparse
        
        class Args:
            input_dir = data_dir
            output_dir = report_dir
            strategy = None
            regime = None
            summary_only = False
            force = True
        
        args = Args()
        
        # Run report generation
        print("\n" + "="*60)
        print("Generating Attribution Reports")
        print("="*60)
        
        # We need to mock sys.argv for the script
        old_argv = sys.argv
        sys.argv = ["attribution_report.py", "--input-dir", str(data_dir), 
                   "--output-dir", str(report_dir), "--force"]
        
        try:
            generate_reports()
        finally:
            sys.argv = old_argv
        
        # Verify reports were created
        print("\n" + "="*60)
        print("Verifying Generated Reports")
        print("="*60)
        
        expected_files = [
            "router_summary.csv",
            "regime_summary.csv",
            "starvation_report.csv",
            "priority_impact_report.csv",
            "trade_performance.csv",
            "merged_summary.csv",
            "SUMMARY.md",
        ]
        
        for filename in expected_files:
            filepath = report_dir / filename
            if filepath.exists():
                print(f"✓ {filename}")
                # Show preview
                if filename.endswith(".csv"):
                    df = pd.read_csv(filepath)
                    print(f"  Rows: {len(df)}, Columns: {list(df.columns)}")
                    if len(df) > 0:
                        print(f"  Preview (first 3 rows):")
                        print(df.head(3).to_string(index=False))
            else:
                print(f"✗ {filename} - MISSING")
        
        # Check visualizations directory
        vis_dir = report_dir / "visualizations"
        if vis_dir.exists():
            print(f"\nVisualizations created: {len(list(vis_dir.glob('*.png')))} PNG files")
        
        # Test strategy detail report
        print("\n" + "="*60)
        print("Testing Strategy Detail Report")
        print("="*60)
        
        # Mock command line for strategy detail
        sys.argv = ["attribution_report.py", "--input-dir", str(data_dir),
                   "--output-dir", str(report_dir), "--strategy", "kbar_feature", "--force"]
        
        try:
            generate_reports()
        finally:
            sys.argv = old_argv
        
        # Check strategy detail
        detail_file = report_dir / "strategy_details" / "kbar_feature_detail.json"
        if detail_file.exists():
            print(f"✓ Strategy detail report created for kbar_feature")
            import json
            with open(detail_file, 'r') as f:
                detail = json.load(f)
                print(f"  Stats: {detail.get('router_stats', {})}")
        
        print("\n" + "="*60)
        print("Test Complete")
        print("="*60)
        print(f"All reports saved to: {report_dir}")


def test_cli_interface():
    """Test the command line interface directly."""
    print("\n" + "="*60)
    print("Testing CLI Interface")
    print("="*60)
    
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        
        # Create sample data
        data_dir = tmpdir / "data"
        data_dir.mkdir()
        create_sample_data(data_dir)
        
        # Test 1: Basic report generation
        print("\nTest 1: Basic report generation")
        report_dir1 = tmpdir / "reports1"
        os.system(f"cd {project_root} && python scripts/attribution_report.py "
                 f"--input-dir {data_dir} --output-dir {report_dir1} --summary-only")
        
        if (report_dir1 / "router_summary.csv").exists():
            print("✓ Basic report generation successful")
        
        # Test 2: Strategy detail
        print("\nTest 2: Strategy detail report")
        report_dir2 = tmpdir / "reports2"
        os.system(f"cd {project_root} && python scripts/attribution_report.py "
                 f"--input-dir {data_dir} --output-dir {report_dir2} --strategy counter_vwap")
        
        detail_file = report_dir2 / "strategy_details" / "counter_vwap_detail.json"
        if detail_file.exists():
            print("✓ Strategy detail report successful")
        
        # Test 3: Regime filter
        print("\nTest 3: Regime-filtered report")
        report_dir3 = tmpdir / "reports3"
        os.system(f"cd {project_root} && python scripts/attribution_report.py "
                 f"--input-dir {data_dir} --output-dir {report_dir3} --regime WEAK")
        
        if (report_dir3 / "router_summary.csv").exists():
            df = pd.read_csv(report_dir3 / "router_summary.csv")
            print(f"✓ Regime-filtered report successful ({len(df)} strategies)")


if __name__ == "__main__":
    print("Attribution Report Test Suite")
    print("="*60)
    
    # Test 1: Create sample data and generate reports
    test_attribution_report()
    
    # Test 2: Test CLI interface
    test_cli_interface()
    
    print("\n" + "="*60)
    print("All Tests Completed Successfully")
    print("="*60)