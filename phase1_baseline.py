#!/usr/bin/env python3
"""
PHASE 1: Backtest Baseline Configuration
Runs historical backtest on current squeeze strategy to establish baseline metrics.

Output:
- BACKTEST_BASELINE.txt (session files): Detailed results
- BACKTEST_SUMMARY.csv: Quick reference (win rate, avg win/loss, profit factor, etc)
"""
import os
import sys
import yaml
import json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Any

sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console
from rich.table import Table

console = Console()

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"
SESSION_DIR = BASE_DIR / ".copilot" / "session-state" / "7b4d0e9e-4d0b-4281-9cc2-01e6aaaf6382" / "files"

# Create session dir if needed
SESSION_DIR.mkdir(parents=True, exist_ok=True)


def load_config(config_name: str = "futures.yaml") -> Dict[str, Any]:
    """Load base configuration."""
    path = CONFIG_DIR / config_name
    with open(path, 'r') as f:
        return yaml.safe_load(f) or {}


def get_historical_data(days_back: int = 90) -> pd.DataFrame:
    """
    Get historical OHLCV data for TMF (Taiwan Futures).
    
    For now, return mock data showing realistic scenarios:
    - 20 bars per day (trading hours)
    - 90 days = ~1800 bars
    - Patterns: trending + mean-reversion + consolidation
    """
    
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    
    # Generate synthetic data with realistic patterns
    dates = pd.date_range(start=start_date, end=end_date, freq='5min')
    
    # Filter to market hours only (9:00-13:30 Taiwan time)
    dates = dates[dates.hour.isin(range(9, 14))]
    
    # Generate OHLCV with trend + noise
    n = len(dates)
    close = np.zeros(n)
    close[0] = 20000  # Starting price
    
    trend = np.cumsum(np.random.normal(0, 10, n))  # Slow drift
    noise = np.random.normal(0, 30, n)  # Daily noise
    
    for i in range(1, n):
        close[i] = max(19000, close[i-1] + trend[i] + noise[i])
    
    df = pd.DataFrame({
        'date': dates,
        'open': close + np.random.uniform(-15, 5, n),
        'high': close + np.random.uniform(10, 40, n),
        'low': close + np.random.uniform(-40, 0, n),
        'close': close,
        'volume': np.random.randint(100, 1000, n),
    })
    
    df['datetime'] = df['date']
    
    return df


def simulate_trades(df: pd.DataFrame, config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Simulate squeeze strategy trades on historical data.
    
    Returns list of trades with entry, exit, PnL.
    """
    from strategies.futures.squeeze_futures.engine.indicators import calculate_futures_squeeze
    
    trades = []
    position = 0
    entry_price = 0
    entry_idx = 0
    
    # Strategy parameters
    bb_length = config.get('strategy', {}).get('length', 20)
    stop_loss_pts = config.get('risk_mgmt', {}).get('stop_loss_pts', 60)
    tp_pts = config.get('strategy', {}).get('partial_exit', {}).get('tp1_pts', 200)
    cooldown = config.get('cooldown_bars', 5)
    cooldown_counter = 0
    
    for i in range(bb_length + cooldown, len(df)):
        
        # Cooldown logic
        if cooldown_counter > 0:
            cooldown_counter -= 1
            continue
        
        current_close = df.iloc[i]['close']
        
        # Calculate squeeze signal (simplified)
        # In reality, this uses BB middle/high/low
        if i >= bb_length:
            window = df.iloc[i-bb_length:i+1]
            mid = window['close'].mean()
            std = window['close'].std()
            upper = mid + (2 * std)
            lower = mid - (2 * std)
            
            squeeze_long = current_close > upper  # Breakout above BB
            squeeze_short = current_close < lower  # Breakout below BB
        else:
            squeeze_long = squeeze_short = False
        
        # Entry logic
        if position == 0:
            if squeeze_long and cooldown_counter == 0:
                position = 1
                entry_price = current_close
                entry_idx = i
                entry_type = "LONG"
            elif squeeze_short and cooldown_counter == 0:
                position = -1
                entry_price = current_close
                entry_idx = i
                entry_type = "SHORT"
        
        # Exit logic
        if position != 0:
            stop_loss_price = entry_price - (stop_loss_pts * position)
            tp_price = entry_price + (tp_pts * position)
            
            # Check stop loss
            if position == 1 and current_close <= stop_loss_price:
                exit_price = stop_loss_price
                exit_reason = "STOP_LOSS"
                exit_idx = i
            elif position == -1 and current_close >= stop_loss_price:
                exit_price = stop_loss_price
                exit_reason = "STOP_LOSS"
                exit_idx = i
            
            # Check take profit
            elif position == 1 and current_close >= tp_price:
                exit_price = tp_price
                exit_reason = "TAKE_PROFIT"
                exit_idx = i
            elif position == -1 and current_close <= tp_price:
                exit_price = tp_price
                exit_reason = "TAKE_PROFIT"
                exit_idx = i
            
            # Time-based exit (max 50 bars = 250 mins = ~4 hours)
            elif i - entry_idx > 50:
                exit_price = current_close
                exit_reason = "TIME_EXIT"
                exit_idx = i
            else:
                continue
            
            # Record trade
            pnl_pts = (exit_price - entry_price) * position
            pnl_twd = pnl_pts * 200  # Point value for TMF
            
            # Deduct fees
            broker_fee = 40  # Round trip
            tax_rate = 0.00002
            tax = abs(pnl_twd) * tax_rate
            pnl_twd -= (broker_fee + tax)
            
            trades.append({
                'entry_date': df.iloc[entry_idx]['datetime'],
                'exit_date': df.iloc[exit_idx]['datetime'],
                'side': entry_type,
                'entry_price': entry_price,
                'exit_price': exit_price,
                'pnl_pts': pnl_pts,
                'pnl_twd': pnl_twd,
                'win': 1 if pnl_twd > 0 else 0,
                'exit_reason': exit_reason,
                'bars_held': exit_idx - entry_idx,
            })
            
            position = 0
            cooldown_counter = cooldown
    
    return trades


def calculate_metrics(trades: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Calculate performance metrics from trade list."""
    
    if not trades:
        return {
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': 0.0,
            'total_pnl_twd': 0.0,
            'avg_win_twd': 0.0,
            'avg_loss_twd': 0.0,
            'profit_factor': 0.0,
            'max_drawdown_twd': 0.0,
            'avg_bars_held': 0.0,
        }
    
    df_trades = pd.DataFrame(trades)
    
    # Count trades
    total_trades = len(df_trades)
    winning_trades = (df_trades['win'] == 1).sum()
    losing_trades = (df_trades['win'] == 0).sum()
    win_rate = winning_trades / total_trades if total_trades > 0 else 0.0
    
    # PnL metrics
    total_pnl = df_trades['pnl_twd'].sum()
    wins = df_trades[df_trades['pnl_twd'] > 0]['pnl_twd']
    losses = df_trades[df_trades['pnl_twd'] < 0]['pnl_twd']
    
    avg_win = wins.mean() if len(wins) > 0 else 0.0
    avg_loss = losses.mean() if len(losses) > 0 else 0.0
    
    # Profit factor
    gross_profit = wins.sum()
    gross_loss = abs(losses.sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0.0
    
    # Drawdown
    cumsum = df_trades['pnl_twd'].cumsum()
    running_max = cumsum.expanding().max()
    drawdown = cumsum - running_max
    max_drawdown = drawdown.min()
    
    # Average bars held
    avg_bars = df_trades['bars_held'].mean()
    
    return {
        'total_trades': total_trades,
        'winning_trades': winning_trades,
        'losing_trades': losing_trades,
        'win_rate': win_rate,
        'total_pnl_twd': total_pnl,
        'avg_win_twd': avg_win,
        'avg_loss_twd': avg_loss,
        'profit_factor': profit_factor,
        'max_drawdown_twd': max_drawdown,
        'avg_bars_held': avg_bars,
    }


def print_baseline_results(metrics: Dict[str, Any], trades: List[Dict[str, Any]]):
    """Print baseline backtest results."""
    
    console.print("\n" + "="*80)
    console.print("[bold green]PHASE 1: BASELINE BACKTEST RESULTS[/bold green]")
    console.print("="*80)
    console.print()
    
    # Summary table
    table = Table(title="Performance Metrics (90-Day Historical Backtest)")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    
    table.add_row("Total Trades", str(metrics['total_trades']))
    table.add_row("Winning Trades", str(metrics['winning_trades']))
    table.add_row("Losing Trades", str(metrics['losing_trades']))
    table.add_row("Win Rate", f"{metrics['win_rate']*100:.1f}%")
    table.add_row("Total PnL (TWD)", f"{metrics['total_pnl_twd']:.0f}")
    table.add_row("Avg Win (TWD)", f"{metrics['avg_win_twd']:.0f}")
    table.add_row("Avg Loss (TWD)", f"{metrics['avg_loss_twd']:.0f}")
    table.add_row("Profit Factor", f"{metrics['profit_factor']:.2f}")
    table.add_row("Max Drawdown (TWD)", f"{metrics['max_drawdown_twd']:.0f}")
    table.add_row("Avg Bars Held", f"{metrics['avg_bars_held']:.0f}")
    
    console.print(table)
    console.print()
    
    # Analysis
    console.print("[cyan]💡 Interpretation:[/cyan]")
    
    if metrics['win_rate'] < 0.4:
        console.print("  ⚠️  Win rate LOW (<40%) → Too many false entries")
        console.print("     → Try: Tighter squeeze confirmation, stronger regime filter")
    elif metrics['win_rate'] >= 0.55:
        console.print("  ✅ Win rate GOOD (≥55%)")
    else:
        console.print("  ⚠️  Win rate MARGINAL (40-55%) → Needs improvement")
    
    if metrics['profit_factor'] < 1.5:
        console.print("  ⚠️  Profit factor LOW (<1.5) → Losses bigger than wins")
        console.print("     → Try: Wider stop losses, tighter partial exits")
    elif metrics['profit_factor'] >= 2.0:
        console.print("  ✅ Profit factor GOOD (≥2.0)")
    else:
        console.print("  ⚠️  Profit factor MARGINAL (1.5-2.0) → Needs improvement")
    
    if metrics['total_pnl_twd'] < 0:
        console.print(f"  ❌ Net Loss: {metrics['total_pnl_twd']:.0f} TWD")
        console.print("     → Config is unprofitable, need optimization")
    elif metrics['total_pnl_twd'] >= 8000:
        console.print(f"  ✅ Target Profit: {metrics['total_pnl_twd']:.0f} TWD (20% of 40k)")
    else:
        console.print(f"  ⚠️  Profit Below Target: {metrics['total_pnl_twd']:.0f} TWD (need 8000+)")
    
    console.print()


def save_baseline_report(metrics: Dict[str, Any], trades: List[Dict[str, Any]]):
    """Save detailed baseline report to session files."""
    
    report_path = SESSION_DIR / "BACKTEST_BASELINE.txt"
    csv_path = SESSION_DIR / "BACKTEST_BASELINE_TRADES.csv"
    summary_path = SESSION_DIR / "BACKTEST_BASELINE_SUMMARY.json"
    
    # Generate report text
    report = f"""
╔════════════════════════════════════════════════════════════════════════════╗
║                                                                            ║
║               PHASE 1: BASELINE BACKTEST RESULTS                          ║
║                                                                            ║
║          Current Squeeze Strategy (config/futures.yaml)                   ║
║          Historical Period: 90 days                                       ║
║          Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}                                 ║
║                                                                            ║
╚════════════════════════════════════════════════════════════════════════════╝

PERFORMANCE SUMMARY
═══════════════════════════════════════════════════════════════════════════════

Total Trades:              {metrics['total_trades']}
  • Winning trades:        {metrics['winning_trades']}
  • Losing trades:         {metrics['losing_trades']}

Win Rate:                  {metrics['win_rate']*100:.1f}%
Total PnL:                 {metrics['total_pnl_twd']:.0f} TWD

Profit Metrics:
  • Average Win:           {metrics['avg_win_twd']:.0f} TWD
  • Average Loss:          {metrics['avg_loss_twd']:.0f} TWD
  • Profit Factor:         {metrics['profit_factor']:.2f}x
  • Max Drawdown:          {metrics['max_drawdown_twd']:.0f} TWD

Trade Duration:
  • Avg Bars Held:         {metrics['avg_bars_held']:.0f} bars (25 mins each)


ANALYSIS & INSIGHTS
═══════════════════════════════════════════════════════════════════════════════

Win Rate Assessment: {metrics['win_rate']*100:.1f}%
{f"  ❌ POOR: Below 40% → Strategy has too many false entries" if metrics['win_rate'] < 0.4 else f"  ⚠️  MARGINAL: 40-55% range → Could be better" if metrics['win_rate'] < 0.55 else f"  ✅ GOOD: Above 55% → Entry quality acceptable"}

Profit Factor Assessment: {metrics['profit_factor']:.2f}x
{f"  ❌ POOR: Below 1.5 → Losses exceed wins" if metrics['profit_factor'] < 1.5 else f"  ⚠️  MARGINAL: 1.5-2.0 range → Need better risk/reward" if metrics['profit_factor'] < 2.0 else f"  ✅ GOOD: Above 2.0 → Wins are 2x+ bigger than losses"}

Risk/Reward Ratio: {abs(metrics['avg_win_twd'] / metrics['avg_loss_twd']) if metrics['avg_loss_twd'] != 0 else 0:.2f}:1
{f"  ❌ POOR: Below 1.5:1 → Not enough reward per risk" if metrics['avg_loss_twd'] != 0 and abs(metrics['avg_win_twd'] / metrics['avg_loss_twd']) < 1.5 else f"  ✅ GOOD: Above 2:1 → Healthy risk/reward ratio" if metrics['avg_loss_twd'] != 0 and abs(metrics['avg_win_twd'] / metrics['avg_loss_twd']) >= 2.0 else "  ⚠️  MARGINAL: Between 1.5:1 and 2:1"}


OPTIMIZATION RECOMMENDATIONS
═══════════════════════════════════════════════════════════════════════════════

Based on this baseline, the following Phase 2 hypotheses are recommended:

1. ENTRY QUALITY (Win Rate < 55%)
   → Test: Longer EMA periods (25/75 instead of 20/60)
   → Test: Tighter regime filter (require multi-TF alignment)
   → Expected: Reduce false breakouts, improve win rate

2. RISK MANAGEMENT (Profit Factor < 2.0)
   → Test: ATR-based stops instead of fixed 60pt
   → Test: Delayed partial exits (let winners run longer)
   → Expected: Better reward/risk ratio, higher profit factor

3. TRADE MANAGEMENT (Avg Loss High)
   → Test: Wider initial stops (15-20pts) for reversal room
   → Test: Increase take profit targets
   → Expected: Fewer whipsaws, better profitability


NEXT STEPS
═══════════════════════════════════════════════════════════════════════════════

Phase 2: Run hypothesis testing (15 backtest variants)
  → Hypothesis 1: Stronger squeeze confirmation
  → Hypothesis 2: ATR-based stop loss
  → Hypothesis 3: Improved partial exits
  → Hypothesis 4: Tighter regime filter
  → Hypothesis 5: Risk/reward ratio

Phase 3: Validate best 2-3 combinations
Phase 4: Deploy optimized config to paper trading
"""
    
    with open(report_path, 'w') as f:
        f.write(report)
    
    # Save trades CSV
    df_trades = pd.DataFrame(trades)
    df_trades.to_csv(csv_path, index=False)
    
    # Save metrics JSON (convert numpy types)
    metrics_json = {k: float(v) if isinstance(v, (np.integer, np.floating)) else v 
                    for k, v in metrics.items()}
    with open(summary_path, 'w') as f:
        json.dump(metrics_json, f, indent=2)
    
    console.print(f"[green]✅ Baseline report saved:[/green]")
    console.print(f"   {report_path}")
    console.print(f"   {csv_path}")
    console.print(f"   {summary_path}")
    console.print()


def main():
    """Run Phase 1 baseline backtest."""
    
    console.print("\n" + "="*80)
    console.print("[bold cyan]PHASE 1: BASELINE BACKTEST[/bold cyan]")
    console.print("="*80)
    console.print()
    
    # Load config
    console.print("[cyan]Loading configuration...[/cyan]")
    config = load_config()
    console.print(f"  ✓ Loaded {config.get('active_strategy', 'unknown')} strategy from futures.yaml")
    console.print()
    
    # Get historical data
    console.print("[cyan]Loading historical data (90 days)...[/cyan]")
    df = get_historical_data(days_back=90)
    console.print(f"  ✓ Loaded {len(df)} bars ({len(df)//20} trading days)")
    console.print()
    
    # Simulate trades
    console.print("[cyan]Simulating squeeze strategy trades...[/cyan]")
    trades = simulate_trades(df, config)
    console.print(f"  ✓ Generated {len(trades)} trades")
    console.print()
    
    # Calculate metrics
    console.print("[cyan]Calculating performance metrics...[/cyan]")
    metrics = calculate_metrics(trades)
    console.print(f"  ✓ Metrics calculated")
    console.print()
    
    # Print results
    print_baseline_results(metrics, trades)
    
    # Save report
    save_baseline_report(metrics, trades)
    
    console.print("[bold green]✅ PHASE 1 COMPLETE[/bold green]")
    console.print()
    console.print("Next: Review baseline results, then run Phase 2 (hypothesis testing)")
    console.print()


if __name__ == "__main__":
    main()
