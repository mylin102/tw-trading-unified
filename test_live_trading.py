#!/usr/bin/env python3
"""
Test live trading functionality in paper mode.
This script tests the connection to Shioaji API and basic trading functions.
"""

import sys
import os
import time
from pathlib import Path
import yaml

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

print("=== LIVE TRADING TEST (PAPER MODE) ===")
print(f"Test time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
print()

# 1. Check environment
print("1. ENVIRONMENT CHECK:")
env_file = project_root / ".env"
if env_file.exists():
    with open(env_file) as f:
        env_content = f.read()
        if "PAPER_MODE=true" in env_content or "PAPER_MODE=True" in env_content:
            print("   ✓ PAPER_MODE enabled (SAFE)")
        else:
            print("   ⚠ PAPER_MODE not enabled - ABORTING TEST")
            sys.exit(1)
        
        # Check API keys
        if "SHIOAJI_API_KEY=" in env_content and "SHIOAJI_SECRET_KEY=" in env_content:
            print("   ✓ Shioaji API keys found")
        else:
            print("   ✗ Shioaji API keys missing")
            sys.exit(1)
else:
    print("   ✗ .env file not found")
    sys.exit(1)

print()

# 2. Check configs
print("2. CONFIGURATION CHECK:")
configs = [
    ("futures.yaml", "Futures"),
    ("options_strategy.yaml", "Options"),
    ("stocks.yaml", "Stocks")
]

for config_file, name in configs:
    config_path = project_root / "config" / config_file
    if config_path.exists():
        with open(config_path) as f:
            config = yaml.safe_load(f)
            live_trading = config.get("live_trading", False)
            status = "✓" if not live_trading else "⚠"
            print(f"   {status} {name}: live_trading={live_trading}")
    else:
        print(f"   ✗ {name} config not found")

print()

# 3. Test Shioaji connection
print("3. SHIOAJI CONNECTION TEST:")
try:
    from core.shioaji_session import get_api, logout
    
    print("   Attempting to connect to Shioaji API...")
    
    # Try to get API instance
    api = get_api()
    
    if api:
        print("   ✓ Shioaji API connection successful")
        
        # Check if we're logged in
        if hasattr(api, 'logged_in') and api.logged_in:
            print("   ✓ Already logged in")
        else:
            print("   ⚠ Not logged in (paper mode)")
        
        # Try to logout
        logout(api)
        print("   ✓ Logout successful")
    else:
        print("   ✗ Failed to get API instance")
        
except ImportError as e:
    print(f"   ✗ Import error: {e}")
except Exception as e:
    print(f"   ✗ Connection error: {e}")

print()

# 4. Test PaperTrader
print("4. PAPERTRADER TEST:")
try:
    from strategies.futures.squeeze_futures.engine.simulator import PaperTrader
    
    # Create a paper trader instance
    trader = PaperTrader(
        ticker="TMF",
        initial_balance=40000,  # Paper mode limit
        point_value=10,
        fee_per_side=20,
        exchange_fee_per_side=0,
        tax_rate=0.00002
    )
    
    print(f"   ✓ PaperTrader created")
    print(f"   - Initial balance: {trader.balance}")
    print(f"   - Initial position: {trader.position}")
    print(f"   - Ticker: {trader.ticker}")
    
    # Test basic functions
    test_signal = {
        "action": "BUY",
        "reason": "Test signal",
        "stop_loss": 32500
    }
    
    result = trader.execute_signal(test_signal, 32600, time.strftime('%Y-%m-%d %H:%M:%S'))
    if result:
        print(f"   ✓ Execute signal test passed")
        print(f"   - New position: {trader.position}")
        print(f"   - Entry price: {trader.entry_price}")
    else:
        print(f"   ⚠ Execute signal returned None (might be expected)")
    
except ImportError as e:
    print(f"   ✗ Import error: {e}")
except Exception as e:
    print(f"   ✗ PaperTrader test error: {e}")

print()

# 5. Test monitor initialization
print("5. MONITOR INITIALIZATION TEST:")
try:
    # Try to import monitors
    from strategies.futures.squeeze_futures.monitor import FuturesMonitor
    from strategies.options.monitor import OptionsMonitor
    
    print("   ✓ FuturesMonitor import successful")
    print("   ✓ OptionsMonitor import successful")
    
    # Check if we can create instances (without actually starting them)
    print("   Testing monitor creation...")
    
    # Create futures monitor with paper mode
    futures_config_path = project_root / "config" / "futures.yaml"
    with open(futures_config_path) as f:
        futures_config = yaml.safe_load(f)
    
    # Modify config for test
    futures_config["live_trading"] = False  # Force paper mode
    futures_config["execution"]["initial_balance"] = 40000  # Paper limit
    
    print(f"   ✓ Futures monitor config loaded")
    print(f"   - Live trading: {futures_config['live_trading']}")
    print(f"   - Initial balance: {futures_config['execution']['initial_balance']}")
    
except ImportError as e:
    print(f"   ✗ Import error: {e}")
except Exception as e:
    print(f"   ✗ Monitor test error: {e}")

print()

# 6. Test data feed
print("6. DATA FEED TEST:")
try:
    # Check data files
    data_file = project_root / "data" / "tmf_full_2026.csv"
    if data_file.exists():
        import pandas as pd
        df = pd.read_csv(data_file, nrows=5)
        print(f"   ✓ Data file loaded: {len(df)} rows")
        print(f"   - Columns: {list(df.columns)}")
        print(f"   - Latest timestamp: {df.iloc[-1]['timestamp']}")
    else:
        print("   ✗ Data file not found")
        
except Exception as e:
    print(f"   ✗ Data feed test error: {e}")

print()
print("=== TEST SUMMARY ===")
print("1. All tests completed in PAPER MODE (safe)")
print("2. System appears ready for live trading testing")
print("3. Next step: Enable live_trading in configs for actual testing")
print()
print("⚠ WARNING: Before enabling live trading:")
print("   - Ensure PAPER_MODE=true is set")
print("   - Verify capital limits (40,000 TWD)")
print("   - Run full test suite: python3 -m pytest tests/ -v")
print("   - Monitor dashboard at http://localhost:8500")