#!/usr/bin/env python3
"""
Fetch near-month and far-month futures data for calendar spread strategies.
Fixed version that handles Shioaji API properly.
"""

import os
import sys

# 2026-07-01 Gemini CLI: Insert project root to path before any core/strategy imports to resolve pathing issues.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
from datetime import datetime, timedelta
from dotenv import load_dotenv
from core.broker.shioaji_compat import kbars_to_dataframe

from strategies.futures.squeeze_futures.data.shioaji_client import ShioajiClient

def get_near_far_contracts(client, category="TMF"):
    """
    Get near-month and far-month contracts for a given category.
    Excludes rolling contracts like TMFR1, TMFR2.
    
    Args:
        client: ShioajiClient instance
        category: Contract category (TMF, TXF, etc.)
        
    Returns:
        tuple: (near_contract, far_contract)
    """
    if not client.is_logged_in:
        print("Client not logged in")
        return None, None
    
    try:
        # Get all contracts for the category
        contracts = list(client.api.Contracts.Futures[category])
        
        # Filter out rolling contracts (R1, R2, etc.)
        regular_contracts = [c for c in contracts if not c.code.endswith(('R1', 'R2', 'R3'))]
        
        # Filter valid contracts (delivery date >= today)
        today_str = datetime.now().strftime("%Y/%m/%d")
        valid_contracts = [c for c in regular_contracts if c.delivery_date >= today_str]
        
        if not valid_contracts:
            print(f"No valid regular contracts for {category}")
            return None, None
        
        # Sort by delivery date
        sorted_contracts = sorted(valid_contracts, key=lambda c: c.delivery_date)
        
        # Near month is the first contract
        near_contract = sorted_contracts[0]
        
        # Far month is the next contract (if available)
        if len(sorted_contracts) > 1:
            far_contract = sorted_contracts[1]
        else:
            # If only one contract, try to get next month
            print(f"Only one valid contract for {category}, checking for next month")
            far_contract = None
            
        print(f"Near contract: {near_contract.code} (delivery: {near_contract.delivery_date})")
        if far_contract:
            print(f"Far contract: {far_contract.code} (delivery: {far_contract.delivery_date})")
        
        return near_contract, far_contract
        
    except Exception as e:
        print(f"Error getting contracts: {e}")
        return None, None

def fetch_kbars_data(client, contract, interval="1min", days=7):
    """
    Fetch K-line data from Shioaji API.
    
    Args:
        client: ShioajiClient instance
        contract: Contract object
        interval: K-line interval (5m, 15m, 1h)
        days: Number of days to fetch
        
    Returns:
        DataFrame with OHLCV data
    """
    if not client.is_logged_in:
        print("Client not logged in")
        return None
    
    try:
        # Calculate start date
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)
        
        # Convert to string format
        start_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        
        print(f"Fetching {contract.code} data from {start_str} to {end_str} ({interval})")
        
        # Fetch K-line data - Shioaji returns a Kbars object
        kbars = client.api.kbars(
            contract=contract,
            start=start_str,
            end=end_str,
        )
        
        # 轉換為DataFrame (使用兼容性助手)
        df = kbars_to_dataframe(kbars)
        
        if df.empty:
            print(f"No data returned for {contract.code}")
            return None
        
        # 將索引 ts 轉換為列
        df = df.reset_index()
        
        print(f"Fetched {len(df)} bars for {contract.code}")
        return df
        
    except Exception as e:
        print(f"Error fetching data for {contract.code}: {e}")
        import traceback
        traceback.print_exc()
        return None

def calculate_spread_metrics(df_near, df_far):
    """
    Calculate spread metrics for calendar spread strategy.
    
    Args:
        df_near: DataFrame with near-month data
        df_far: DataFrame with far-month data
        
    Returns:
        DataFrame with spread metrics
    """
    # Merge data on timestamp
    df_merged = pd.merge(
        df_near[['ts', 'Close']],
        df_far[['ts', 'Close']],
        on='ts',
        suffixes=('_near', '_far')
    )
    
    # Calculate spread
    df_merged['spread'] = df_merged['Close_near'] - df_merged['Close_far']
    
    # Calculate rolling statistics
    window = 20  # 20-period rolling window
    df_merged['spread_ma'] = df_merged['spread'].rolling(window=window, min_periods=window).mean()
    df_merged['spread_std'] = df_merged['spread'].rolling(window=window, min_periods=window).std()
    
    # Calculate z-score
    safe_spread_std = df_merged['spread_std'].replace(0, pd.NA)
    df_merged['spread_z'] = (df_merged['spread'] - df_merged['spread_ma']) / safe_spread_std
    
    # 2026-07-09 Hermes Agent: Calculate Spread EMA 20 and EMA 60 for trend direction
    df_merged['spread_ema_20'] = df_merged['spread'].ewm(span=20, adjust=False).mean()
    df_merged['spread_ema_60'] = df_merged['spread'].ewm(span=60, adjust=False).mean()
    
    # Add VWAP for near month (simplified as rolling mean)
    df_merged['vwap'] = df_near['Close'].rolling(window=window, min_periods=window).mean()
    df_merged['vwap_std'] = df_near['Close'].rolling(window=window, min_periods=window).std()
    
    # Calculate VWAP z-score
    safe_vwap_std = df_merged['vwap_std'].replace(0, pd.NA)
    df_merged['vwap_z'] = (df_merged['Close_near'] - df_merged['vwap']) / safe_vwap_std
    
    return df_merged

def main():
    """Main function to fetch and process calendar spread data."""
    # Load environment variables
    load_dotenv()
    
    # Initialize client
    client = ShioajiClient()
    
    # Login
    if not client.login():
        print("Failed to login to Shioaji")
        return
    
    # ── Config Loading (GSD: Zero Hardcoding) ──
    # 2026-07-01 Gemini CLI: Resolve active ticker dynamically from futures/futures_night.yaml config to prevent MXF hardcoding.
    import yaml
    from core.date_utils import is_night_session
    _is_night = is_night_session(datetime.now())
    cfg_name = "futures_night.yaml" if _is_night else "futures.yaml"
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cfg_path = os.path.join(project_root, "config", cfg_name)
    if os.path.exists(cfg_path):
        with open(cfg_path, "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    else:
        cfg = {}
    ticker = cfg.get("ticker")
    if not ticker:
        raise ValueError(f"ERROR: 'ticker' missing in {cfg_name}")

    # Get near and far contracts (excluding rolling contracts)
    near_contract, far_contract = get_near_far_contracts(client, ticker)
    
    if not near_contract or not far_contract:
        print(f"Failed to get near or far contracts for {ticker}")
        return
    
    print(f"Near contract: {near_contract.code} (delivery: {near_contract.delivery_date})")
    print(f"Far contract: {far_contract.code} (delivery: {far_contract.delivery_date})")
    
    # Fetch data for both contracts
    print("\nFetching near-month data...")
    df_near = fetch_kbars_data(client, near_contract, interval="1min", days=7)
    
    print("\nFetching far-month data...")
    df_far = fetch_kbars_data(client, far_contract, interval="1min", days=7)
    
    if df_near is None or df_far is None:
        print("Failed to fetch data")
        return
    
    # Calculate spread metrics
    print("\nCalculating spread metrics...")
    df_spread = calculate_spread_metrics(df_near, df_far)
    
    # Save data
    output_dir = "./data"
    os.makedirs(output_dir, exist_ok=True)
    
    # Save individual contract data
    today = datetime.now().strftime("%Y%m%d")
    ticker_lower = ticker.lower()
    df_near.to_csv(f"{output_dir}/{ticker_lower}_near_{today}.csv", index=False)
    df_far.to_csv(f"{output_dir}/{ticker_lower}_far_{today}.csv", index=False)
    
    # Save spread data
    df_spread.to_csv(f"{output_dir}/{ticker_lower}_calendar_spread_{today}.csv", index=False)
    
    print(f"\nData saved to {output_dir}/")
    print(f"Near contract: {near_contract.code}")
    print(f"Far contract: {far_contract.code}")
    print(f"Near data shape: {df_near.shape}")
    print(f"Far data shape: {df_far.shape}")
    print(f"Spread data shape: {df_spread.shape}")
    
    # Show sample of spread data
    print("\nSample spread data (last 5 rows):")
    print(df_spread[['ts', 'Close_near', 'Close_far', 'spread', 'spread_z', 'vwap_z']].tail())
    
    # Analyze spread characteristics
    print("\nSpread analysis:")
    print(f"Mean spread: {df_spread['spread'].mean():.2f}")
    print(f"Spread std: {df_spread['spread'].std():.2f}")
    print(f"Max spread z-score: {df_spread['spread_z'].max():.2f}")
    print(f"Min spread z-score: {df_spread['spread_z'].min():.2f}")
    print(f"Entries with spread_z > 2: {(df_spread['spread_z'] > 2).sum()}")
    print(f"Entries with spread_z < -2: {(df_spread['spread_z'] < -2).sum()}")
    
    # Check for trading signals
    print("\nPotential trading signals (last 20 periods):")
    recent = df_spread.tail(20)
    
    # SELL_NEAR_BUY_FAR signal (spread too high)
    sell_signals = recent[(recent['vwap_z'] >= 2.0) & (recent['spread_z'] >= 2.0)]
    if not sell_signals.empty:
        print(f"SELL_NEAR_BUY_FAR signals: {len(sell_signals)}")
        for _, row in sell_signals.iterrows():
            print(f"  {row['ts']}: vwap_z={row['vwap_z']:.2f}, spread_z={row['spread_z']:.2f}")
    
    # BUY_NEAR_SELL_FAR signal (spread too low)
    buy_signals = recent[(recent['vwap_z'] <= -2.0) & (recent['spread_z'] <= -2.0)]
    if not buy_signals.empty:
        print(f"BUY_NEAR_SELL_FAR signals: {len(buy_signals)}")
        for _, row in buy_signals.iterrows():
            print(f"  {row['ts']}: vwap_z={row['vwap_z']:.2f}, spread_z={row['spread_z']:.2f}")

if __name__ == "__main__":
    main()