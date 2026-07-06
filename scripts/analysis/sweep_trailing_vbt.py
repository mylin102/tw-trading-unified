#!/usr/bin/env python3
"""
Vectorized Parameter Sweep for Trailing Stop optimization using vectorbt.
Follows GSD Wave 5.4.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pandas as pd
import numpy as np
import vectorbt as vbt
from core.data_manager import data_manager
from strategies.futures.entry_strategies import strategy_cumulative_delta
from core.strategy_registry import StrategyRegistry
from core.strategy_context import StrategyContext, MarketData, PositionView

from core.date_utils import get_session

# Configuration
INITIAL_CAPITAL = 1_000_000
POINT_VALUE = 200 # TMF

def run_vbt_sweep(strategy_name: str, session_type: int = None):
    session_name = "ALL" if session_type is None else ("DAY" if session_type == 1 else "NIGHT")
    print(f"\n🚀 Starting vectorbt sweep for: {strategy_name} (Session: {session_name})")
    
    # 1. Load Data
    df = data_manager.load_historical("TXFR1")
    if df.empty:
        print("❌ Data not found")
        return
    
    # Use sufficient data for statistical significance
    df = df.iloc[-100000:]
    
    # Filter by session if specified
    if session_type is not None:
        # Create session mask
        df['session'] = [get_session(ts) for ts in df.index]
        df = df[df['session'] == session_type]
        print(f"  Filtered to {len(df)} bars for {session_name} session")

    # 2. Pre-calculate Signals
    reg = StrategyRegistry()
    reg.discover()
    strategy = reg.get(strategy_name)
    
    entries = pd.Series(False, index=df.index)
    exits = pd.Series(False, index=df.index)
    
    print("🧠 Generating raw signals...")
    strategy.init(StrategyContext(
        market=MarketData(last_bar={}, df_5m=df.iloc[:50]),
        position=PositionView(size=0),
        config={}
    ))
    
    for i in range(50, len(df)):
        ctx = StrategyContext(
            market=MarketData(last_bar=df.iloc[i].to_dict(), df_5m=df.iloc[i-50:i+1]),
            position=PositionView(size=0),
            config={},
            bar_counter=i
        )
        sig = strategy.on_bar(ctx)
        if sig:
            if sig.action == "BUY": entries.iloc[i] = True
            elif sig.action == "SELL": exits.iloc[i] = True
            
    # 3. Define Parameter Grid
    be_triggers = np.linspace(10, 100, 10)
    trail_points = np.linspace(20, 150, 10)
    
    print(f"📊 Sweeping {len(be_triggers) * len(trail_points)} combinations...")
    
    results = []
    close = df['Close']
    
    for be in be_triggers:
        for tp in trail_points:
            pf = vbt.Portfolio.from_signals(
                close, entries, exits,
                sl_stop=tp/close.iloc[0], 
                init_cash=INITIAL_CAPITAL,
                fees=0.0002,
                freq='5T'
            )
            stats = pf.stats()
            results.append({
                'be_trigger': be,
                'trail_pts': tp,
                'total_return': stats['Total Return [%]'],
                'sharpe': stats['Sharpe Ratio'],
                'win_rate': stats['Win Rate [%]'],
                'pf': stats['Profit Factor']
            })

    res_df = pd.DataFrame(results)
    best = res_df.sort_values('total_return', ascending=False).iloc[0]
    
    print("\n" + "="*40)
    print(f"🏆 BEST PARAMS for {strategy_name} ({session_name})")
    print(f"BE Trigger: {best['be_trigger']:.0f} pts")
    print(f"Trail Pts:  {best['trail_pts']:.0f} pts")
    print(f"Return:     {best['total_return']:.2f}%")
    print("="*40)
    
    return best

if __name__ == "__main__":
    report = []
    for s in ["counter_vwap", "cumulative_delta"]:
        for session in [1, 2]: # 1=Day, 2=Night
            best = run_vbt_sweep(s, session)
            report.append({
                "strategy": s,
                "session": "DAY" if session == 1 else "NIGHT",
                "be": best['be_trigger'],
                "trail": best['trail_pts'],
                "return": best['total_return']
            })
    
    print("\n📊 SESSION COMPARISON SUMMARY")
    print(pd.DataFrame(report).to_string(index=False))
