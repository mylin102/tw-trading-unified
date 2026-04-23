#!/usr/bin/env python3
"""
Attribution-Enabled Backtest Runner

Runs futures strategy router with attribution tracking enabled.
Generates attribution CSV files for analysis.

Usage:
    python scripts/attribution_backtest.py --data ./data/tmf_full_2026.csv --output ./exports/attribution_backtest
"""

import sys
import os
import pandas as pd
import numpy as np
from datetime import datetime
from pathlib import Path
import argparse
import time
from typing import Dict, List, Any, Optional
import yaml

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, PositionView, MarketData
from core.signal import Signal
from core.backtest_engine import BacktestEngine, AssetProfile, AssetType
from core.futures_bar_regime import FuturesBarRegimeResult
from core.futures_strategy_router import route_futures_signal
from core.attribution_recorder import AttributionRecorder
from strategies.futures.monitor import FuturesMonitor


# ==================== Configuration ====================
START_DATE = '2025-01-01'
END_DATE = '2026-04-01'
INITIAL_CAPITAL = 1_000_000

# Asset Profiles
FUTURES_PROFILE = AssetProfile(
    asset_type=AssetType.FUTURES,
    point_value=200,
    margin_per_lot=170000,
    fee_rate=0.00002,
    tax_rate=0.00002
)


def load_futures_data(data_path: Path) -> pd.DataFrame:
    """Load futures data with proper timestamp handling."""
    df = pd.read_csv(data_path)
    
    # Ensure timestamp column exists
    if 'timestamp' not in df.columns and 'datetime' in df.columns:
        df = df.rename(columns={'datetime': 'timestamp'})
    
    # Convert timestamp
    df['timestamp'] = pd.to_datetime(df['timestamp'])
    df = df.sort_values('timestamp').reset_index(drop=True)
    
    # Filter date range
    mask = (df['timestamp'] >= START_DATE) & (df['timestamp'] <= END_DATE)
    df = df[mask].copy()
    
    print(f"Loaded {len(df)} bars from {df['timestamp'].min()} to {df['timestamp'].max()}")
    return df


def create_mock_monitor() -> FuturesMonitor:
    """Create a mock monitor for backtesting."""
    class MockAPI:
        pass
    
    class MockConfig:
        def get(self, key, default=None):
            return default
    
    # Create minimal config
    config = {
        "strategy": {
            "regime_filter": "mid",
            "pullback": {"ema_fast": 20, "ema_slow": 60},
        },
        "risk_mgmt": {
            "atr_multiplier": 1.2,
            "atr_length": 14,
        },
        "trade_mgmt": {},
        "execution": {},
        "monitoring": {
            "poll_interval_secs": 30,
            "stale_tick_warn_secs": 120,
            "stale_tick_critical_secs": 600,
        }
    }
    
    # Write temp config
    config_path = Path("/tmp/attribution_backtest_config.yaml")
    with open(config_path, 'w') as f:
        yaml.dump(config, f)
    
    # Create monitor
    monitor = FuturesMonitor(MockAPI(), str(config_path), dry_run=True)
    
    # Mock trader
    class MockTrader:
        position = 0
        entry_price = 0.0
        current_stop_loss = 0.0
        current_take_profit = 0.0
    
    monitor.trader = MockTrader()
    monitor._registry = StrategyRegistry()
    
    return monitor


def run_attribution_backtest(df: pd.DataFrame, output_dir: Path) -> Dict[str, Any]:
    """Run backtest with attribution tracking."""
    print(f"\n{'='*60}")
    print("Running Attribution-Enabled Backtest")
    print(f"{'='*60}")
    
    # Create output directory
    output_dir.mkdir(parents=True, exist_ok=True)
    attribution_dir = output_dir / "attribution_data"
    attribution_dir.mkdir(exist_ok=True)
    
    # Initialize attribution recorder
    recorder = AttributionRecorder(
        output_dir=attribution_dir,
        buffer_size=100,  # Small buffer for testing
        flush_interval_seconds=300
    )
    
    # Create mock monitor
    monitor = create_mock_monitor()
    
    # Initialize registry with futures strategies
    registry = StrategyRegistry()
    # Set config directory
    config_dir = Path(__file__).parent.parent / "config" / "strategies"
    registry.discover(config_dir=config_dir)
    
    # Initialize strategies
    for name in registry.list_all():
        strategy = registry.get(name["name"])
        if strategy:
            # Create initial context for init
            init_ctx = StrategyContext(
                market=MarketData(
                    timestamp=df.iloc[0]['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
                    last_bar={},
                    df_5m=None,
                    df_15m=None,
                    session=1,
                    regime="NEUTRAL"
                ),
                position=PositionView(),
                config={},
                bar_counter=0
            )
            strategy.init(init_ctx)
    
    monitor._registry = registry
    
    # Track results
    trades = []
    signals = []
    
    # Simulate bar-by-bar processing
    for i, row in df.iterrows():
        if i % 100 == 0:
            print(f"Processing bar {i}/{len(df)}...")
        
        # Create context with indicators in last_bar
        last_bar = {
            "Close": row['Close'],
            "High": row['High'],
            "Low": row['Low'],
            "Open": row['Open'],
            "Volume": row.get('Volume', 1000),
            "symbol": "TX",
            # Add indicators needed by strategies
            "adx": 25.0,
            "vwap": row['Close'] * 0.995,
            "atr": 100.0,
            "score": -15.0,
            "regime": "WEAK",
            "bear_align": False,
            "bearish_align": False,
            "bull_align": False,
            "bullish_align": False,
            "macd_hist": -5.0,
            "macd_rising": False,
            "mom_velo": -2.0,
            "recent_high": row['High'] + 50,
            "recent_low": row['Low'] - 50,
            "price_vs_vwap": -0.005,
            "volume_spike": False
        }
        
        ctx = StrategyContext(
            market=MarketData(
                timestamp=row['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
                last_bar=last_bar,
                df_5m=None,
                df_15m=None,
                session=1,
                regime="WEAK"
            ),
            position=PositionView(
                size=monitor.trader.position,
                entry_price=monitor.trader.entry_price,
                current_stop_loss=monitor.trader.current_stop_loss,
                unrealized_pnl=0.0,
                has_tp1_hit=False
            ),
            config={},
            bar_counter=i
        )
        
        # Create regime result
        regime_result = FuturesBarRegimeResult(
            regime="WEAK",
            bias="SHORT",
            confidence=0.7,
            reasons=["mock backtest"],
            session_regime="WEAK"
        )
        
        # Route signal with attribution
        decision = route_futures_signal(
            registry=registry,
            context=ctx,
            regime_result=regime_result,
            active_strategy_name=None,
            current_working_orders=[],
            is_flattening=False,
            prepare_strategy=lambda name, strategy: None,
            recorder=recorder
        )
        
        # Log signal if generated
        if decision and decision.is_trade and decision.signal:
            signals.append({
                "timestamp": row['timestamp'],
                "strategy": decision.selected_strategy or 'unknown',
                "action": decision.signal.action if decision.signal else 'unknown',
                "reason": decision.signal.reason if decision.signal else '',
                "stop_loss": decision.signal.stop_loss if decision.signal else 0.0
            })
            
            # Simulate trade (simple mock)
            if len(trades) < 20:  # Limit number of trades for demo
                entry_price = row['Close']
                exit_price = entry_price * (1.01 if decision.signal.action == 'BUY' else 0.99)
                pnl = (exit_price - entry_price) * 200 * (1 if decision.signal.action == 'BUY' else -1)
                
                trade = {
                    "trade_id": f"T{len(trades):03d}",
                    "timestamp": row['timestamp'],
                    "strategy": decision.selected_strategy or 'unknown',
                    "action": decision.signal.action if decision.signal else 'unknown',
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "exit_reason": "target"
                }
                trades.append(trade)
                
                # Log trade to attribution
                recorder.log_trade(
                    trade_id=trade['trade_id'],
                    symbol="TX",
                    strategy_name=trade['strategy'],
                    regime_at_entry="WEAK",
                    side=trade['action'],
                    entry_time=trade['timestamp'].strftime('%Y-%m-%d %H:%M:%S'),
                    exit_time=(trade['timestamp'] + pd.Timedelta(hours=1)).strftime('%Y-%m-%d %H:%M:%S'),
                    entry_price=trade['entry_price'],
                    exit_price=trade['exit_price'],
                    pnl=trade['pnl'],
                    exit_reason=trade['exit_reason']
                )
    
    # Force final export
    recorder.export_csv_if_needed(force=True)
    
    # Generate reports
    print(f"\n{'='*60}")
    print("Generating Attribution Reports")
    print(f"{'='*60}")
    
    report_dir = output_dir / "reports"
    report_dir.mkdir(exist_ok=True)
    
    # Run attribution report script
    import subprocess
    cmd = [
        sys.executable, "scripts/attribution_report.py",
        "--input-dir", str(attribution_dir),
        "--output-dir", str(report_dir),
        "--force"
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        print("Attribution reports generated successfully")
        print(result.stdout)
    else:
        print("Error generating reports:")
        print(result.stderr)
    
    # Summary
    print(f"\n{'='*60}")
    print("Backtest Summary")
    print(f"{'='*60}")
    print(f"Total bars processed: {len(df)}")
    print(f"Signals generated: {len(signals)}")
    print(f"Trades simulated: {len(trades)}")
    print(f"Total PnL: ${sum(t['pnl'] for t in trades):.2f}")
    print(f"\nAttribution data saved to: {attribution_dir}")
    print(f"Reports saved to: {report_dir}")
    
    return {
        "signals": signals,
        "trades": trades,
        "attribution_dir": attribution_dir,
        "report_dir": report_dir
    }


def main():
    parser = argparse.ArgumentParser(description="Run attribution-enabled backtest")
    parser.add_argument("--data", type=Path, default=Path("./data/tmf_full_2026.csv"),
                       help="Path to futures data CSV")
    parser.add_argument("--output", type=Path, default=Path("./exports/attribution_backtest"),
                       help="Output directory for results")
    parser.add_argument("--sample", type=int, default=1000,
                       help="Number of bars to sample (for quick testing)")
    
    args = parser.parse_args()
    
    # Check if data exists
    if not args.data.exists():
        print(f"Error: Data file not found: {args.data}")
        print("Please provide a valid CSV file with columns: timestamp, Open, High, Low, Close, Volume")
        sys.exit(1)
    
    # Load data
    print(f"Loading data from {args.data}...")
    df = load_futures_data(args.data)
    
    # Sample if requested
    if args.sample and args.sample < len(df):
        df = df.iloc[:args.sample].copy()
        print(f"Sampled {args.sample} bars for quick testing")
    
    # Run backtest
    results = run_attribution_backtest(df, args.output)
    
    # Save summary
    summary_path = args.output / "backtest_summary.json"
    import json
    with open(summary_path, 'w') as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "bars_processed": len(df),
            "signals_generated": len(results["signals"]),
            "trades_simulated": len(results["trades"]),
            "total_pnl": sum(t["pnl"] for t in results["trades"]),
            "attribution_data_dir": str(results["attribution_dir"]),
            "report_dir": str(results["report_dir"])
        }, f, indent=2, default=str)
    
    print(f"\nBacktest complete! Summary saved to {summary_path}")
    
    # Show quick analysis
    if results["signals"]:
        print(f"\nSignal distribution:")
        signal_df = pd.DataFrame(results["signals"])
        print(signal_df["strategy"].value_counts())
    
    if results["trades"]:
        print(f"\nTrade performance:")
        trade_df = pd.DataFrame(results["trades"])
        print(trade_df.groupby("strategy").agg({
            "pnl": ["count", "sum", "mean"]
        }).round(2))


if __name__ == "__main__":
    main()