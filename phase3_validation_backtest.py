#!/usr/bin/env python3
"""
Phase 3: Validation Backtest
Tests optimized config (merged from Phase 2 top 3 variants)
against 90-day historical synthetic data before paper trading.

Config: config/futures_optimized.yaml
Parameters:
  - cooldown_bars: 20 (from H5.3: 100% WR)
  - bb_length: 25 (from H1.2: 83.3% WR, 14.31x PF)
  - tp_pts: 300 (from H3.3: 9.96x PF, 215,755 TWD)

Expected Results:
  - Win Rate: 75-85% (between H5 and H1)
  - Profit Factor: 7-14x
  - Total PnL: 120k-160k TWD (exceeds 20% target)
  - Trade Count: 4-8 trades
"""

import os
import sys
import yaml
import json
import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

BASE_DIR = Path(__file__).parent
CONFIG_DIR = BASE_DIR / "config"


def get_synthetic_data(days_back: int = 90) -> pd.DataFrame:
    """Generate synthetic OHLC data for backtest."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days_back)
    dates = pd.date_range(start=start_date, end=end_date, freq='5min')
    dates = dates[dates.hour.isin(range(9, 14))]
    
    n = len(dates)
    close = np.zeros(n)
    close[0] = 20000
    
    trend = np.cumsum(np.random.normal(0, 10, n))
    noise = np.random.normal(0, 30, n)
    
    for i in range(1, n):
        close[i] = max(19000, close[i-1] + trend[i] + noise[i])
    
    high = close + np.abs(np.random.normal(0, 20, n))
    low = close - np.abs(np.random.normal(0, 20, n))
    volume = np.random.randint(100, 1000, n)
    
    return pd.DataFrame({
        'timestamp': dates,
        'open': close * (1 + np.random.normal(0, 0.001, n)),
        'high': high,
        'low': low,
        'close': close,
        'volume': volume,
    })


def calculate_bollinger_bands(df: pd.DataFrame, length: int = 20, mult: float = 2.0):
    """Calculate Bollinger Bands."""
    df['bb_mid'] = df['close'].rolling(length).mean()
    bb_std = df['close'].rolling(length).std()
    df['bb_upper'] = df['bb_mid'] + (bb_std * mult)
    df['bb_lower'] = df['bb_mid'] - (bb_std * mult)
    return df


def calculate_atr(df: pd.DataFrame, length: int = 14):
    """Calculate ATR."""
    df['tr'] = np.maximum(
        df['high'] - df['low'],
        np.maximum(
            abs(df['high'] - df['close'].shift(1)),
            abs(df['low'] - df['close'].shift(1))
        )
    )
    df['atr'] = df['tr'].rolling(length).mean()
    return df


def backtest_config(config: dict, df: pd.DataFrame) -> dict:
    """Run simple backtest with config."""
    
    # Prices and position tracking
    prices = df['close'].values
    atr_vals = df['atr'].values
    bb_upper = df['bb_upper'].values
    bb_lower = df['bb_lower'].values
    
    cooldown_bars = config['cooldown_bars']
    tp_pts = config['strategy']['partial_exit']['tp1_pts']
    stop_loss_pts = config['risk_mgmt']['stop_loss_pts']
    
    position = 0
    entry_price = 0
    trades = []
    cooldown_counter = 0
    capital = config['execution']['initial_balance']
    broker_fee = config['execution']['broker_fee_per_side']
    tax_rate = config['execution']['tax_rate']
    
    for i in range(20, len(prices) - 1):
        price = prices[i]
        atr = atr_vals[i] if atr_vals[i] > 0 else stop_loss_pts
        
        # Update cooldown
        if cooldown_counter > 0:
            cooldown_counter -= 1
        
        # Exit logic
        if position != 0:
            exit_price = None
            exit_reason = None
            
            # Take profit
            if position > 0 and price >= entry_price + tp_pts:
                exit_price = entry_price + tp_pts
                exit_reason = "TP"
            elif position < 0 and price <= entry_price - tp_pts:
                exit_price = entry_price - tp_pts
                exit_reason = "TP"
            
            # Stop loss
            if position > 0 and price <= entry_price - stop_loss_pts:
                exit_price = entry_price - stop_loss_pts
                exit_reason = "SL"
            elif position < 0 and price >= entry_price + stop_loss_pts:
                exit_price = entry_price + stop_loss_pts
                exit_reason = "SL"
            
            if exit_price is not None:
                # Calculate PnL
                if position > 0:
                    pnl = (exit_price - entry_price) * 200  # TMF point value
                else:
                    pnl = (entry_price - exit_price) * 200
                
                # Deduct fees and tax
                pnl -= 2 * broker_fee  # Round-trip
                pnl -= abs(pnl) * tax_rate
                
                capital += pnl
                
                trades.append({
                    'entry': entry_price,
                    'exit': exit_price,
                    'pnl': pnl,
                    'direction': 'LONG' if position > 0 else 'SHORT',
                    'reason': exit_reason,
                })
                
                position = 0
                cooldown_counter = cooldown_bars
        
        # Entry logic (simple squeeze breakout)
        if position == 0 and cooldown_counter == 0:
            squeeze = (bb_upper[i] - bb_lower[i]) < (bb_upper[max(0, i-10)] - bb_lower[max(0, i-10)])
            
            if squeeze and price > bb_upper[max(0, i-1)]:
                entry_price = price
                position = 1
            elif squeeze and price < bb_lower[max(0, i-1)]:
                entry_price = price
                position = -1
    
    # Calculate metrics
    total_pnl = sum(t['pnl'] for t in trades)
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] < 0]
    
    win_rate = len(wins) / len(trades) if trades else 0
    avg_win = sum(t['pnl'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['pnl'] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(t['pnl'] for t in wins)) / abs(sum(t['pnl'] for t in losses)) if losses and sum(t['pnl'] for t in losses) != 0 else 0
    
    return {
        'trades': trades,
        'total_pnl': total_pnl,
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'num_trades': len(trades),
        'final_capital': capital,
    }


def run_phase3_validation():
    """Run backtest with optimized config."""
    
    print("\n" + "="*80)
    print("PHASE 3: VALIDATION BACKTEST")
    print("="*80)
    print(f"Time: {datetime.now().isoformat()}")
    print(f"Config: config/futures_optimized.yaml")
    print()
    
    # Load optimized config
    try:
        with open(CONFIG_DIR / 'futures_optimized.yaml', 'r') as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"❌ Failed to load config: {e}")
        return False
    
    print("OPTIMIZED PARAMETERS:")
    print(f"  cooldown_bars: {config['cooldown_bars']} (from H5.3, was 5)")
    print(f"  bb_length: {config['strategy']['spring_upthrust']['bb_length']} (from H1.2, was 20)")
    print(f"  tp_pts: {config['strategy']['partial_exit']['tp1_pts']} (from H3.3, was 200)")
    print()
    
    # Generate synthetic data
    print(f"Generating 90-day synthetic data (5-min bars)...")
    df = get_synthetic_data(days_back=90)
    
    # Calculate indicators
    df = calculate_bollinger_bands(df, length=config['strategy']['spring_upthrust']['bb_length'])
    df = calculate_atr(df)
    
    print(f"Generated {len(df)} bars")
    print()
    
    # Run backtest
    print("Running backtest...")
    try:
        results = backtest_config(config, df)
        
        total_pnl = results['total_pnl']
        win_rate = results['win_rate']
        profit_factor = results['profit_factor']
        num_trades = results['num_trades']
        avg_win = results['avg_win']
        avg_loss = results['avg_loss']
        
        # Print results
        print()
        print("="*80)
        print("BACKTEST RESULTS")
        print("="*80)
        print(f"Total Trades: {num_trades}")
        print(f"Win Rate: {win_rate*100:.1f}%")
        print(f"Profit Factor: {profit_factor:.2f}x")
        print(f"Total PnL: {total_pnl:,.0f} TWD")
        print(f"Avg Win: {avg_win:,.0f} TWD")
        print(f"Avg Loss: {avg_loss:,.0f} TWD")
        print()
        
        # Validate against Phase 3 targets
        print("="*80)
        print("PHASE 3 VALIDATION GATES")
        print("="*80)
        
        gates = {
            "Win Rate ≥ 50%": win_rate >= 0.50,
            "Profit Factor ≥ 3.0x": profit_factor >= 3.0,
            "Total PnL ≥ 8000 TWD (20%)": total_pnl >= 8000,
            "Trade Count ≥ 3": num_trades >= 3,
        }
        
        all_pass = True
        for gate, passed in gates.items():
            status = "✅ PASS" if passed else "❌ FAIL"
            print(f"{status}: {gate}")
            if not passed:
                all_pass = False
        
        print()
        
        # Comparison with Phase 2 results
        print("="*80)
        print("VS PHASE 2 HYPOTHESIS RESULTS")
        print("="*80)
        print(f"H5.3-Extended_Cooldown:  100% WR | 138,096 TWD (4 trades)")
        print(f"H1.2-Longer_EMA:         83.3% WR | 160,216 TWD | 14.31x PF")
        print(f"H3.3-Late_30pts:         66.7% WR | 215,755 TWD | 9.96x PF")
        print()
        print(f"Phase 3 Merged:          {win_rate*100:.1f}% WR | {total_pnl:,.0f} TWD | {profit_factor:.2f}x PF ({num_trades} trades)")
        print()
        
        # Decision
        if all_pass:
            print("="*80)
            print("✅ PHASE 3 BACKTEST: PASS")
            print("="*80)
            print("Configuration validated. Ready to proceed with Phase 3 paper trading.")
            print()
            print("Next Steps:")
            print("1. Start live paper trading with config/futures_optimized.yaml")
            print("2. Collect minimum 50 real trades over 1-2 weeks")
            print("3. Monitor: Win rate, PnL, execution quality")
            print("4. Gate: If 20%+ cumulative profit achieved → Phase 4 go-live")
            print()
            return True
        else:
            print("="*80)
            print("⚠️  PHASE 3 BACKTEST: CONDITIONAL")
            print("="*80)
            print("Some gates failed. Review parameter tuning.")
            print("Suggest: Adjust bb_length, cooldown_bars, or tp_pts and re-run.")
            print()
            return False
            
    except Exception as e:
        print(f"❌ Backtest failed: {e}")
        import traceback
        traceback.print_exc()
        return False


if __name__ == "__main__":
    success = run_phase3_validation()
    sys.exit(0 if success else 1)
