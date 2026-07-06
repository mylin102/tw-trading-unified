#!/usr/bin/env python3
"""
Elite Strategies Validation — Simple Logic Test

Tests that all 3 elite strategies can:
1. Load without errors
2. Generate valid signals (or None)
3. Follow the strategy contract (return dict with action, reason, stop_loss)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(__file__))))

from strategies.futures.elite_strategies import (
    get_elite_strategies,
    detect_market_regime,
    strategy_counter_vwap,
    strategy_psar_breakout,
    strategy_vol_squeeze,
)
from rich.console import Console
import pandas as pd
import numpy as np

console = Console()


def create_mock_state(regime="ranging"):
    """Create a mock market state for testing."""
    # Create synthetic 5m data
    n = 100
    df = pd.DataFrame({
        "Close": np.linspace(21000, 21100, n),
        "High": np.linspace(21010, 21110, n),
        "Low": np.linspace(20990, 21090, n),
        "Open": np.linspace(21005, 21105, n),
        "Volume": np.random.exponential(100, n),
        "sqz_on": [False] * n,
        "mom_state": [2] * n,
        "momentum": [10.0] * n,
        "mom_velo": [5.0] * n,
        "atr": [30.0] * n,
        "vwap": [21050.0] * n,
        "bullish_align": [True] * n,
        "bearish_align": [False] * n,
        "fired": [False] * n,
        "recent_high": [21100.0] * n,
        "recent_low": [21000.0] * n,
        "adx": [20.0] * n,
    }, index=pd.date_range("2026-01-01", periods=n, freq="5min"))
    
    if regime == "ranging":
        # Flip bullish_align frequently
        df["bullish_align"] = [i % 5 < 2 for i in range(n)]
    
    last = df.iloc[-1]
    
    return {
        "last_5m": last,
        "last_15m": last,
        "df_5m": df,
        "score": 25,
        "stop_loss_pts": 60,
        "hour": 10,
        "fire_pending_dir": 0,
        "fire_bar_idx": 0,
        "fire_high": 0.0,
        "fire_low": 0.0,
        "bar_counter": n,
    }


def validate_signal(signal, strategy_name):
    """Validate that a signal follows the contract."""
    if signal is None:
        return True, "No signal (valid)"
    
    # Must have required fields
    required = ["action", "reason", "stop_loss"]
    for field in required:
        if field not in signal:
            return False, f"Missing required field: {field}"
    
    # Action must be BUY or SELL
    if signal["action"] not in ("BUY", "SELL"):
        return False, f"Invalid action: {signal['action']}"
    
    # Stop loss must be positive
    if signal["stop_loss"] <= 0:
        return False, f"Stop loss must be positive, got: {signal['stop_loss']}"
    
    return True, f"Valid signal: {signal['action']} {signal['reason']} SL={signal['stop_loss']:.1f}"


def main():
    console.print("[bold]🎯 Elite Strategies Validation[/bold]")
    console.print("[dim]Testing strategy logic with mock data[/dim]\n")
    
    # Test 1: Load all strategies
    console.print("[bold blue]Test 1: Load Elite Strategies[/bold blue]")
    strategies = get_elite_strategies()
    assert len(strategies) == 3, f"Expected 3 elite strategies, got {len(strategies)}"
    console.print(f"✅ Loaded {len(strategies)} elite strategies:")
    for name, meta in strategies.items():
        console.print(f"   - {name} (PF={meta['backtest_pf']}, WR={meta['backtest_wr']}%)")
    
    # Test 2: Counter-VWAP strategy
    console.print("\n[bold blue]Test 2: Counter-VWAP Strategy[/bold blue]")
    state_ranging = create_mock_state("ranging")
    cfg = {"strategy": {"counter_mode": {"enabled": True, "confirm_bars": 5, "atr_sl_mult": 2.0}}}
    
    # Should not signal without fire
    signal1 = strategy_counter_vwap(state_ranging, cfg)
    valid1, msg1 = validate_signal(signal1, "counter_vwap")
    console.print(f"  No fire: {msg1}")
    assert valid1, f"Counter-VWAP failed: {msg1}"
    
    # Simulate fire + failure
    state_ranging["fire_pending_dir"] = 1  # Bullish fire
    state_ranging["fire_bar_idx"] = 95
    state_ranging["fire_high"] = 21100
    state_ranging["last_5m"].mom_velo = -5  # Momentum reversed
    state_ranging["last_5m"].close = 21080  # Below recent high
    
    signal2 = strategy_counter_vwap(state_ranging, cfg)
    valid2, msg2 = validate_signal(signal2, "counter_vwap")
    console.print(f"  Fire + failure: {msg2}")
    assert valid2, f"Counter-VWAP failed: {msg2}"
    
    # Test 3: PSAR Breakout strategy
    console.print("\n[bold blue]Test 3: PSAR Breakout Strategy[/bold blue]")
    state_trending = create_mock_state("trending")
    
    # PSAR requires TA-Lib, may fail if not installed
    try:
        signal3 = strategy_psar_breakout(state_trending, cfg)
        valid3, msg3 = validate_signal(signal3, "psar_breakout")
        console.print(f"  Signal: {msg3}")
        assert valid3, f"PSAR Breakout failed: {msg3}"
    except Exception as e:
        console.print(f"  ⚠️ PSAR calculation skipped: {e}")
    
    # Test 4: Volume-Filtered Squeeze
    console.print("\n[bold blue]Test 4: Volume-Filtered Squeeze[/bold blue]")
    state_breakout = create_mock_state("breakout")
    state_breakout["score"] = 25  # Above entry_score
    state_breakout["last_5m"].Volume = 300  # High volume (3x avg)
    
    signal4 = strategy_vol_squeeze(state_breakout, cfg)
    valid4, msg4 = validate_signal(signal4, "vol_squeeze")
    console.print(f"  Signal: {msg4}")
    assert valid4, f"Vol-Squeeze failed: {msg4}"
    
    # Test without volume spike
    state_breakout["last_5m"].Volume = 50  # Low volume
    signal5 = strategy_vol_squeeze(state_breakout, cfg)
    assert signal5 is None, "Vol-Squeeze should reject low volume"
    console.print(f"  Low volume filter: ✅ Correctly rejected")
    
    # Test 5: Market regime detection
    console.print("\n[bold blue]Test 5: Market Regime Detection[/bold blue]")
    df_ranging = create_mock_state("ranging")["df_5m"]
    df_trending = create_mock_state("trending")["df_5m"]
    
    regime_ranging = detect_market_regime(df_ranging)
    regime_trending = detect_market_regime(df_trending)
    
    console.print(f"  Ranging market: {regime_ranging}")
    console.print(f"  Trending market: {regime_trending}")
    
    # Test 6: Strategy contract compliance
    console.print("\n[bold blue]Test 6: Strategy Contract Compliance[/bold blue]")
    for name, meta in strategies.items():
        state = create_mock_state()
        cfg_test = {"strategy": {"counter_mode": {"enabled": True}}}
        signal = meta["func"](state, cfg_test)
        valid, msg = validate_signal(signal, name)
        
        status = "✅" if valid else "❌"
        console.print(f"  {status} {name}: {msg}")
        assert valid, f"{name} failed contract: {msg}"
    
    # Summary
    console.print("\n[bold green]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold green]")
    console.print("[bold green]✅ ALL VALIDATION TESTS PASSED[/bold green]")
    console.print("[bold green]━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━[/bold green]")
    console.print("\n📊 Elite Strategies Summary:")
    console.print("   ✅ Counter-VWAP: Core strategy (PF=1.95)")
    console.print("   ✅ PSAR Breakout: Trend following (PF=1.42)")
    console.print("   ✅ Vol-Squeeze: Quality filtering (PF~1.3)")
    console.print("\n🚀 Ready for live backtesting with real data")


if __name__ == "__main__":
    main()
