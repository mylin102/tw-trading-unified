#!/usr/bin/env python3
"""
Phase 3 Paper Trading Monitor
Real-time tracking and analysis of Phase 3 validation trades.

Features:
- Monitor cumulative PnL
- Track win rate
- Alert on capital limits
- Daily summary reports
- Success gate validation
"""

import os
import sys
import pandas as pd
from datetime import datetime
from pathlib import Path


def load_trade_log(csv_path: str = "logs/trade_log.csv") -> pd.DataFrame:
    """Load trade log from CSV."""
    if not os.path.exists(csv_path):
        return pd.DataFrame(columns=['timestamp', 'entry_price', 'exit_price', 'quantity', 'pnl', 'reason'])
    return pd.read_csv(csv_path)


def calculate_metrics(trades: pd.DataFrame) -> dict:
    """Calculate trading metrics."""
    if trades.empty:
        return {
            'total_trades': 0,
            'total_pnl': 0,
            'win_rate': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'profit_factor': 0,
            'wins': 0,
            'losses': 0,
        }
    
    total_pnl = trades['pnl'].sum()
    wins = (trades['pnl'] > 0).sum()
    losses = (trades['pnl'] < 0).sum()
    win_rate = wins / len(trades) if len(trades) > 0 else 0
    
    winning_trades = trades[trades['pnl'] > 0]['pnl']
    losing_trades = trades[trades['pnl'] < 0]['pnl']
    
    avg_win = winning_trades.mean() if len(winning_trades) > 0 else 0
    avg_loss = losing_trades.mean() if len(losing_trades) > 0 else 0
    
    profit_factor = abs(winning_trades.sum() / losing_trades.sum()) if len(losing_trades) > 0 and losing_trades.sum() != 0 else 0
    
    return {
        'total_trades': len(trades),
        'total_pnl': total_pnl,
        'win_rate': win_rate,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': profit_factor,
        'wins': wins,
        'losses': losses,
    }


def check_success_gates(metrics: dict) -> dict:
    """Check if Phase 3 success gates are met."""
    gates = {
        'pnl_target': metrics['total_pnl'] >= 8000,  # ≥20% of 40k
        'win_rate_target': metrics['win_rate'] >= 0.50,
        'trade_count': metrics['total_trades'] >= 50,
        'profit_factor': metrics['profit_factor'] >= 3.0,
    }
    return gates


def display_status(trades: pd.DataFrame):
    """Display real-time status."""
    metrics = calculate_metrics(trades)
    gates = check_success_gates(metrics)
    
    print("\n" + "="*80)
    print("PHASE 3 PAPER TRADING MONITOR")
    print("="*80)
    print(f"Time: {datetime.now().isoformat()}")
    print(f"Config: config/futures.yaml")
    print()
    
    # Current metrics
    print("CURRENT METRICS")
    print("-"*80)
    print(f"Total Trades: {metrics['total_trades']}")
    print(f"Cumulative PnL: {metrics['total_pnl']:,.0f} TWD")
    print(f"Win Rate: {metrics['win_rate']*100:.1f}% ({metrics['wins']} wins, {metrics['losses']} losses)")
    print(f"Profit Factor: {metrics['profit_factor']:.2f}x")
    print(f"Avg Win: {metrics['avg_win']:,.0f} TWD")
    print(f"Avg Loss: {metrics['avg_loss']:,.0f} TWD")
    print()
    
    # Success gates
    print("PHASE 3 SUCCESS GATES")
    print("-"*80)
    status_pnl = "✅ PASS" if gates['pnl_target'] else f"⏳ {metrics['total_pnl']:,.0f} / 8,000 TWD"
    status_wr = "✅ PASS" if gates['win_rate_target'] else f"⏳ {metrics['win_rate']*100:.1f}% / 50%"
    status_trades = "✅ PASS" if gates['trade_count'] else f"⏳ {metrics['total_trades']} / 50 trades"
    status_pf = "✅ PASS" if gates['profit_factor'] else f"⏳ {metrics['profit_factor']:.2f}x / 3.0x"
    
    print(f"PnL ≥ 8,000 TWD (20%): {status_pnl}")
    print(f"Win Rate ≥ 50%: {status_wr}")
    print(f"Trade Count ≥ 50: {status_trades}")
    print(f"Profit Factor ≥ 3.0x: {status_pf}")
    print()
    
    # Overall status
    all_gates_pass = all(gates.values())
    if metrics['total_trades'] >= 50:
        print("="*80)
        if all_gates_pass:
            print("✅ PHASE 3 SUCCESS - ALL GATES PASS")
            print("Ready for Phase 4: Go-Live Readiness")
        else:
            print("⚠️  PHASE 3 INCOMPLETE - Some gates failing")
            print("Recommendation: Adjust parameters and retry Phase 3")
        print("="*80)
    else:
        print("="*80)
        print(f"🔄 PHASE 3 IN PROGRESS ({metrics['total_trades']}/50 trades)")
        print("="*80)
    
    print()


def main():
    """Main monitoring loop."""
    csv_path = "logs/trade_log.csv"
    
    # Initial load
    trades = load_trade_log(csv_path)
    display_status(trades)
    
    # Show recent trades
    if len(trades) > 0:
        print("\nRECENT TRADES (last 10)")
        print("-"*80)
        recent = trades.tail(10)[['timestamp', 'entry_price', 'exit_price', 'pnl', 'reason']]
        print(recent.to_string(index=False))
        print()


if __name__ == "__main__":
    main()
