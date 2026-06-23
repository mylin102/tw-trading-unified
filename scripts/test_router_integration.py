#!/usr/bin/env python3
"""
Test script to verify router functionality and attribution integration.
"""

import sys
import os
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path("/Users/mylin/Documents/mylin102/tw-trading-unified")
sys.path.insert(0, str(PROJECT_ROOT))

def test_router_functionality():
    """Test if router is properly integrated and functional."""
    
    print("=" * 70)
    print("Router Functionality Test")
    print("=" * 70)
    
    # Test 1: Check if monitor has router support
    print("\n1. Checking monitor.py router support...")
    try:
        from strategies.futures.monitor import FuturesMonitor
        
        # Check if monitor has _route_signal method
        monitor = FuturesMonitor()
        if hasattr(monitor, '_route_signal'):
            print("✅ monitor.py has _route_signal method")
        else:
            print("❌ monitor.py missing _route_signal method")
            
        # Check if monitor accepts attribution_recorder
        import inspect
        sig = inspect.signature(monitor._route_signal)
        params = list(sig.parameters.keys())
        if 'attribution_recorder' in params:
            print("✅ _route_signal accepts attribution_recorder parameter")
        else:
            print("❌ _route_signal missing attribution_recorder parameter")
            
    except Exception as e:
        print(f"❌ Error checking monitor: {e}")
    
    # Test 2: Check router module
    print("\n2. Checking router module...")
    try:
        from core.futures_strategy_router import route_strategy
        
        # Create a mock bar
        mock_bar = {
            'timestamp': '2026-04-23 00:45:00',
            'open': 20000,
            'high': 20050,
            'low': 19950,
            'close': 20025,
            'volume': 1000,
            'symbol': 'TX'
        }
        
        # Test router with mock data
        result = route_strategy(
            bar=mock_bar,
            regime="WEAK",
            config={'strategy': {'auto_select': True}}
        )
        
        if result and hasattr(result, 'candidates'):
            print(f"✅ Router returns {len(result.candidates)} candidates")
            for i, candidate in enumerate(result.candidates):
                print(f"   Candidate {i+1}: {candidate}")
        else:
            print("❌ Router returned no candidates")
            
    except Exception as e:
        print(f"❌ Error testing router: {e}")
    
    # Test 3: Check configuration
    print("\n3. Checking system configuration...")
    try:
        import yaml
        
        with open(PROJECT_ROOT / 'config' / 'futures.yaml', 'r') as f:
            config = yaml.safe_load(f)
        
        # Check key settings
        issues = []
        
        if config.get('live_trading') == False:
            print("⚠️  live_trading: false (paper mode)")
        else:
            print("✅ live_trading: true (live mode)")
        
        strategy_config = config.get('strategy', {})
        
        if strategy_config.get('auto_select') == False:
            issues.append("auto_select: false (single strategy mode)")
        else:
            print("✅ auto_select: true (multi-strategy mode)")
        
        if 'strategy_list' not in strategy_config:
            issues.append("Missing strategy_list")
        else:
            print(f"✅ strategy_list: {strategy_config.get('strategy_list')}")
        
        if issues:
            print("❌ Configuration issues found:")
            for issue in issues:
                print(f"   - {issue}")
        else:
            print("✅ Configuration looks good")
            
    except Exception as e:
        print(f"❌ Error checking configuration: {e}")
    
    # Test 4: Check attribution data
    print("\n4. Checking attribution data...")
    try:
        import pandas as pd
        
        data_dir = PROJECT_ROOT / 'data' / 'attribution' / 'night_session'
        router_file = data_dir / 'router_evaluation_log.csv'
        
        if router_file.exists():
            df = pd.read_csv(router_file)
            print(f"✅ Attribution data: {len(df)} rows")
            print(f"   Strategies: {df['strategy_name'].nunique()}")
            print(f"   Unique strategies: {df['strategy_name'].unique().tolist()}")
            
            # Check if data looks real or simulated
            notes = df['notes'].iloc[0] if len(df) > 0 else ''
            if 'simulated' in notes.lower() or 'test' in notes.lower():
                print("⚠️  Data appears to be simulated/test data")
            else:
                print("✅ Data appears to be real")
        else:
            print("❌ No attribution data found")
            
    except Exception as e:
        print(f"❌ Error checking attribution data: {e}")
    
    # Test 5: Check actual trading logs
    print("\n5. Checking trading logs...")
    try:
        log_file = PROJECT_ROOT / 'logs' / 'pm2-trading-out-3.log'
        
        if log_file.exists():
            # Count recent entries
            import subprocess
            result = subprocess.run(
                ['tail', '-100', str(log_file)],
                capture_output=True,
                text=True
            )
            
            logs = result.stdout
            
            # Check for key patterns
            patterns = {
                'candidates': 'candidates=',
                'router': 'router',
                'entry': 'entry',
                'signal': 'signal=',
                'trade': 'trade'
            }
            
            for pattern, desc in patterns.items():
                count = logs.lower().count(pattern)
                if count > 0:
                    print(f"✅ Found {count} '{desc}' references in recent logs")
                else:
                    print(f"⚠️  No '{desc}' references in recent logs")
        else:
            print("❌ Trading log not found")
            
    except Exception as e:
        print(f"❌ Error checking logs: {e}")
    
    print("\n" + "=" * 70)
    print("Test Complete")
    print("=" * 70)

def suggest_fixes():
    """Suggest fixes based on test results."""
    
    print("\n" + "=" * 70)
    print("Suggested Fixes")
    print("=" * 70)
    
    fixes = []
    
    # Fix 1: Enable multi-strategy mode
    fixes.append("""
1. Enable multi-strategy mode in config/futures.yaml:
   
   Change:
     strategy:
       auto_select: false
   
   To:
     strategy:
       auto_select: true
       strategy_list: [counter_vwap, spring_upthrust, kbar_feature]
   
   Command:
     python3 -c "
     import yaml
     with open('config/futures.yaml', 'r') as f:
         config = yaml.safe_load(f)
     config['strategy']['auto_select'] = True
     config['strategy']['strategy_list'] = ['counter_vwap', 'spring_upthrust', 'kbar_feature']
     with open('config/futures.yaml', 'w') as f:
         yaml.dump(config, f, default_flow_style=False)
     print('Configuration updated')
     "
    """)
    
    # Fix 2: Verify router is being called
    fixes.append("""
2. Verify router is being called in monitor.py:
   
   Check if _route_signal is called in the main loop:
   
   Command:
     grep -n "self._route_signal" strategies/futures/monitor.py
   
   Expected: Should find calls in process_bar or similar methods
    """)
    
    # Fix 3: Check attribution integration
    fixes.append("""
3. Ensure attribution_recorder is passed to router:
   
   Check if attribution_recorder parameter is used:
   
   Command:
     grep -A10 "def _route_signal" strategies/futures/monitor.py | grep -n "attribution_recorder"
   
   Expected: Should see attribution_recorder being passed to route_strategy
    """)
    
    # Fix 4: Test with simple script
    fixes.append("""
4. Test router directly:
   
   Create test script:
   
   ```python
   from core.futures_strategy_router import route_strategy
   
   mock_bar = {
       'timestamp': '2026-04-23 00:50:00',
       'open': 20000,
       'high': 20050,
       'low': 19950,
       'close': 20025,
       'volume': 1000,
       'symbol': 'TX'
   }
   
   result = route_strategy(bar=mock_bar, regime="WEAK")
   print(f"Candidates: {result.candidates if result else 'None'}")
   ```
    """)
    
    for i, fix in enumerate(fixes, 1):
        print(f"\nFix #{i}:")
        print(fix)
    
    print("\n" + "=" * 70)
    print("After applying fixes, restart the trading system:")
    print("  pm2 restart trading")
    print("=" * 70)

if __name__ == "__main__":
    test_router_functionality()
    suggest_fixes()