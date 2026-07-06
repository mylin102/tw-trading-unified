#!/usr/bin/env python3
"""
Calendar Condor Backtest with Contract Resolver

This script:
1. Uses ContractResolver to get proper near/far month contracts
2. Fetches historical data for both contracts
3. Calculates spread metrics
4. Runs backtest for calendar_condor_v2 strategy
"""

import os
import sys
import argparse
import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv

# Add project root to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import contract resolver
try:
    from core.contract_resolver import ContractResolver
    CONTRACT_RESOLVER_AVAILABLE = True
except ImportError:
    CONTRACT_RESOLVER_AVAILABLE = False
    print("ContractResolver not available, using fallback")

# Import Shioaji
try:
    import shioaji as sj
    SHIOAJI_AVAILABLE = True
except ImportError:
    SHIOAJI_AVAILABLE = False
    print("Shioaji not available, using mock data")


def fetch_calendar_data_with_resolver(start_date: str, end_date: str, product: str = "TMF"):
    """
    Fetch calendar spread data using ContractResolver.
    
    Args:
        start_date: Start date (YYYY-MM-DD)
        end_date: End date (YYYY-MM-DD)
        product: Product code (TMF, TXF, etc.)
        
    Returns:
        Tuple of (df_near, df_far, near_contract, far_contract)
    """
    if not SHIOAJI_AVAILABLE or not CONTRACT_RESOLVER_AVAILABLE:
        print("Shioaji or ContractResolver not available, using mock data")
        return _create_mock_data(start_date, end_date)
    
    # Load environment variables
    load_dotenv()
    api_key = os.getenv('SHIOAJI_API_KEY')
    secret_key = os.getenv('SHIOAJI_SECRET_KEY')
    
    if not api_key or not secret_key:
        print("Missing Shioaji API credentials")
        return _create_mock_data(start_date, end_date)
    
    try:
        # Initialize Shioaji API
        api = sj.Shioaji()
        api.login(api_key=api_key, secret_key=secret_key, fetch_contract=True)
        
        # Initialize ContractResolver
        resolver = ContractResolver(api)
        
        # Get near and far contracts
        near_contract, far_contract = resolver.get_near_far_contracts(product)
        
        if not near_contract or not far_contract:
            print("Failed to get near/far contracts")
            return None, None, None, None
        
        print(f"Selected contracts:")
        print(f"  Near: {near_contract.code} (delivery: {near_contract.delivery_date})")
        print(f"  Far: {far_contract.code} (delivery: {far_contract.delivery_date})")
        
        # Fetch data
        print(f"Fetching data from {start_date} to {end_date}")
        df_near = resolver.fetch_kbars(near_contract, start_date, end_date)
        df_far = resolver.fetch_kbars(far_contract, start_date, end_date)
        
        if df_near.empty or df_far.empty:
            print("Failed to fetch data")
            return None, None, None, None
        
        print(f"Data fetched: near={len(df_near)} bars, far={len(df_far)} bars")
        
        return df_near, df_far, near_contract, far_contract
        
    except Exception as e:
        print(f"Error fetching data: {e}")
        import traceback
        traceback.print_exc()
        return None, None, None, None


def calculate_spread_features(df_near: pd.DataFrame, df_far: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """
    Calculate spread features for calendar spread strategy.
    
    Args:
        df_near: DataFrame with near-month data
        df_far: DataFrame with far-month data
        window: Rolling window size
        
    Returns:
        DataFrame with spread features
    """
    if df_near.empty or df_far.empty:
        print("Empty dataframes provided")
        return pd.DataFrame()
    
    # Merge data on timestamp
    df_merged = pd.merge(
        df_near[['ts', 'Close']],
        df_far[['ts', 'Close']],
        on='ts',
        suffixes=('_near', '_far')
    )
    
    if df_merged.empty:
        print("No overlapping timestamps between near and far contracts")
        return pd.DataFrame()
    
    # Calculate spread
    df_merged['spread'] = df_merged['Close_near'] - df_merged['Close_far']
    
    # Calculate rolling statistics for spread
    df_merged['spread_ma'] = df_merged['spread'].rolling(window=window, min_periods=window).mean()
    df_merged['spread_std'] = df_merged['spread'].rolling(window=window, min_periods=window).std()
    
    # Calculate spread z-score
    safe_spread_std = df_merged['spread_std'].replace(0, pd.NA)
    df_merged['spread_z'] = (df_merged['spread'] - df_merged['spread_ma']) / safe_spread_std
    
    # Calculate VWAP for near month (simplified as rolling mean)
    df_merged['vwap'] = df_near['Close'].rolling(window=window, min_periods=window).mean()
    df_merged['vwap_std'] = df_near['Close'].rolling(window=window, min_periods=window).std()
    
    # Calculate VWAP z-score
    safe_vwap_std = df_merged['vwap_std'].replace(0, pd.NA)
    df_merged['vwap_z'] = (df_merged['Close_near'] - df_merged['vwap']) / safe_vwap_std
    
    # Calculate price vs VWAP
    df_merged['price_vs_vwap'] = df_merged['Close_near'] - df_merged['vwap']
    
    # Add regime placeholder (will be calculated by regime classifier)
    df_merged['regime'] = 'WEAK'  # Placeholder
    
    # Add other required features
    df_merged['adx'] = 20.0  # Placeholder
    df_merged['breakout_strength'] = 0.3  # Placeholder
    df_merged['volume_spike'] = 1.0  # Placeholder
    
    # Add session info
    df_merged['is_night_session'] = False  # Placeholder
    df_merged['bars_from_session_open'] = 10  # Placeholder
    
    return df_merged


def _create_mock_data(start_date: str, end_date: str):
    """Create mock data for testing when Shioaji is not available."""
    print("Creating mock data for testing")
    
    # Generate date range
    start = datetime.strptime(start_date, "%Y-%m-%d")
    end = datetime.strptime(end_date, "%Y-%m-%d")
    
    # Create 5-minute intervals
    dates = pd.date_range(start=start, end=end, freq='5min')
    
    # Create mock near month data
    base_price = 21000
    df_near = pd.DataFrame({
        'ts': dates,
        'Open': base_price + pd.Series(range(len(dates))) * 0.1,
        'High': base_price + pd.Series(range(len(dates))) * 0.1 + 5,
        'Low': base_price + pd.Series(range(len(dates))) * 0.1 - 5,
        'Close': base_price + pd.Series(range(len(dates))) * 0.1 + 2,
        'Volume': 1000 + pd.Series(range(len(dates))) % 500,
    })
    
    # Create mock far month data (slightly different)
    df_far = pd.DataFrame({
        'ts': dates,
        'Open': base_price + 50 + pd.Series(range(len(dates))) * 0.08,
        'High': base_price + 50 + pd.Series(range(len(dates))) * 0.08 + 4,
        'Low': base_price + 50 + pd.Series(range(len(dates))) * 0.08 - 4,
        'Close': base_price + 50 + pd.Series(range(len(dates))) * 0.08 + 1,
        'Volume': 800 + pd.Series(range(len(dates))) % 400,
    })
    
    # Create mock contracts
    class MockContract:
        def __init__(self, code, delivery_date):
            self.code = code
            self.delivery_date = delivery_date
    
    near_contract = MockContract("TMFE6", "2026/05/20")
    far_contract = MockContract("TMFF6", "2026/06/17")
    
    return df_near, df_far, near_contract, far_contract


def run_backtest(df_features: pd.DataFrame, initial_capital: float = 100000):
    """
    Run simple backtest for calendar spread strategy.
    
    Args:
        df_features: DataFrame with spread features
        initial_capital: Initial capital in TWD
        
    Returns:
        Dictionary with backtest results
    """
    if df_features.empty:
        return {"error": "Empty dataframe"}
    
    # Strategy parameters
    entry_vwap_z = 2.0
    entry_spread_z = 2.0
    exit_spread_z = 0.5
    stop_loss_spread_z = 2.5
    position_size = 1
    
    # Trading state
    position = 0  # 0: flat, 1: long spread, -1: short spread
    entry_price = 0.0
    entry_idx = 0
    capital = initial_capital
    trades = []
    
    # Commission and fees (8 points round-trip per contract)
    commission_per_contract = 8.0
    
    for i, row in df_features.iterrows():
        # Skip early rows without enough data
        if pd.isna(row['spread_z']) or pd.isna(row['vwap_z']):
            continue
        
        # Check exit conditions if in position
        if position != 0:
            bars_held = i - entry_idx
            
            # Stop loss
            if position == -1 and row['spread_z'] > stop_loss_spread_z:  # Short spread stop loss
                exit_price = row['spread']
                pnl = (entry_price - exit_price) * position_size - commission_per_contract
                capital += pnl
                trades.append({
                    'entry_idx': entry_idx,
                    'exit_idx': i,
                    'side': 'SHORT_SPREAD',
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'pnl': pnl,
                    'reason': 'stop_loss'
                })
                position = 0
                
            elif position == 1 and row['spread_z'] < -stop_loss_spread_z:  # Long spread stop loss
                exit_price = row['spread']
                pnl = (exit_price - entry_price) * position_size - commission_per_contract
                capital += pnl
                trades.append({
                    'entry_idx': entry_idx,
                    'exit_idx': i,
                    'side': 'LONG_SPREAD',
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'pnl': pnl,
                    'reason': 'stop_loss'
                })
                position = 0
            
            # Profit target
            elif position == -1 and row['spread_z'] < exit_spread_z:  # Short spread profit
                exit_price = row['spread']
                pnl = (entry_price - exit_price) * position_size - commission_per_contract
                capital += pnl
                trades.append({
                    'entry_idx': entry_idx,
                    'exit_idx': i,
                    'side': 'SHORT_SPREAD',
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'pnl': pnl,
                    'reason': 'profit'
                })
                position = 0
                
            elif position == 1 and row['spread_z'] > -exit_spread_z:  # Long spread profit
                exit_price = row['spread']
                pnl = (exit_price - entry_price) * position_size - commission_per_contract
                capital += pnl
                trades.append({
                    'entry_idx': entry_idx,
                    'exit_idx': i,
                    'side': 'LONG_SPREAD',
                    'entry_price': entry_price,
                    'exit_price': exit_price,
                    'pnl': pnl,
                    'reason': 'profit'
                })
                position = 0
        
        # Check entry conditions if flat
        if position == 0:
            # Check for short spread entry (sell near, buy far)
            if row['vwap_z'] > entry_vwap_z and row['spread_z'] > entry_spread_z:
                position = -1  # Short spread
                entry_price = row['spread']
                entry_idx = i
                
            # Check for long spread entry (buy near, sell far)
            elif row['vwap_z'] < -entry_vwap_z and row['spread_z'] < -entry_spread_z:
                position = 1  # Long spread
                entry_price = row['spread']
                entry_idx = i
    
    # Close any open position at the end
    if position != 0:
        last_row = df_features.iloc[-1]
        exit_price = last_row['spread']
        
        if position == -1:  # Short spread
            pnl = (entry_price - exit_price) * position_size - commission_per_contract
        else:  # Long spread
            pnl = (exit_price - entry_price) * position_size - commission_per_contract
        
        capital += pnl
        trades.append({
            'entry_idx': entry_idx,
            'exit_idx': len(df_features) - 1,
            'side': 'SHORT_SPREAD' if position == -1 else 'LONG_SPREAD',
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl': pnl,
            'reason': 'end_of_period'
        })
    
    # Calculate performance metrics
    total_trades = len(trades)
    winning_trades = [t for t in trades if t['pnl'] > 0]
    losing_trades = [t for t in trades if t['pnl'] <= 0]
    
    win_rate = len(winning_trades) / total_trades if total_trades > 0 else 0
    total_pnl = sum(t['pnl'] for t in trades)
    avg_win = sum(t['pnl'] for t in winning_trades) / len(winning_trades) if winning_trades else 0
    avg_loss = sum(t['pnl'] for t in losing_trades) / len(losing_trades) if losing_trades else 0
    
    # Calculate drawdown
    equity_curve = [initial_capital]
    for trade in trades:
        equity_curve.append(equity_curve[-1] + trade['pnl'])
    
    peak = initial_capital
    max_drawdown = 0
    for equity in equity_curve:
        if equity > peak:
            peak = equity
        drawdown = (peak - equity) / peak * 100
        if drawdown > max_drawdown:
            max_drawdown = drawdown
    
    return {
        'initial_capital': initial_capital,
        'final_capital': capital,
        'total_pnl': total_pnl,
        'total_trades': total_trades,
        'winning_trades': len(winning_trades),
        'losing_trades': len(losing_trades),
        'win_rate': win_rate * 100,
        'avg_win': avg_win,
        'avg_loss': avg_loss,
        'profit_factor': abs(avg_win / avg_loss) if avg_loss != 0 else float('inf'),
        'max_drawdown': max_drawdown,
        'trades': trades,
    }


def main():
    """Main function."""
    parser = argparse.ArgumentParser(description='Calendar Condor Backtest')
    parser.add_argument('--start', type=str, default='2026-04-01', help='Start date (YYYY-MM-DD)')
    parser.add_argument('--end', type=str, default='2026-04-22', help='End date (YYYY-MM-DD)')
    parser.add_argument('--product', type=str, default='TMF', help='Product code (TMF, TXF, etc.)')
    parser.add_argument('--capital', type=float, default=100000, help='Initial capital')
    
    args = parser.parse_args()
    
    print(f"Calendar Condor Backtest")
    print(f"Period: {args.start} to {args.end}")
    print(f"Product: {args.product}")
    print(f"Initial capital: {args.capital:,.0f} TWD")
    print("-" * 50)
    
    # Fetch data
    df_near, df_far, near_contract, far_contract = fetch_calendar_data_with_resolver(
        args.start, args.end, args.product
    )
    
    if df_near is None or df_far is None:
        print("Failed to fetch data")
        return
    
    # Calculate spread features
    df_features = calculate_spread_features(df_near, df_far)
    
    if df_features.empty:
        print("Failed to calculate features")
        return
    
    print(f"Features calculated: {len(df_features)} rows")
    print(f"Spread range: {df_features['spread'].min():.2f} to {df_features['spread'].max():.2f}")
    print(f"Spread z-score range: {df_features['spread_z'].min():.2f} to {df_features['spread_z'].max():.2f}")
    
    # Run backtest
    results = run_backtest(df_features, args.capital)
    
    # Print results
    print("\n" + "=" * 50)
    print("BACKTEST RESULTS")
    print("=" * 50)
    print(f"Initial Capital: {results['initial_capital']:,.0f} TWD")
    print(f"Final Capital: {results['final_capital']:,.0f} TWD")
    print(f"Total PnL: {results['total_pnl']:,.0f} TWD")
    print(f"Total Trades: {results['total_trades']}")
    print(f"Winning Trades: {results['winning_trades']}")
    print(f"Losing Trades: {results['losing_trades']}")
    print(f"Win Rate: {results['win_rate']:.1f}%")
    print(f"Average Win: {results['avg_win']:,.0f} TWD")
    print(f"Average Loss: {results['avg_loss']:,.0f} TWD")
    print(f"Profit Factor: {results['profit_factor']:.2f}")
    print(f"Max Drawdown: {results['max_drawdown']:.1f}%")
    
    # Print trade details
    if results['trades']:
        print("\nTrade Details:")
        for i, trade in enumerate(results['trades'], 1):
            print(f"  Trade {i}: {trade['side']} | Entry: {trade['entry_price']:.2f} | "
                  f"Exit: {trade['exit_price']:.2f} | PnL: {trade['pnl']:,.0f} TWD | "
                  f"Reason: {trade['reason']}")


if __name__ == "__main__":
    main()