import os
import sys
import time
import pandas as pd
from pathlib import Path
from rich.console import Console

# Add project root to path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))

from core.shioaji_session import get_api, logout
from strategies.stocks.monitor import StockMonitor

console = Console()

def diagnose():
    console.print("[bold cyan]🔍 Starting Stock Monitor Diagnosis...[/bold cyan]")
    
    strategy_override = None
    if len(sys.argv) > 1:
        strategy_override = sys.argv[1]
        console.print(f"[yellow]Using strategy override: {strategy_override}[/yellow]")

    api = None
    try:
        # 1. Test API Connection
        console.print("[cyan]Step 1: Testing Shioaji Login...[/cyan]")
        api = get_api()
        console.print(f"[green]✅ Shioaji Login Success (User: {api.list_accounts()[0].person_id})[/green]")
        
        # 2. Initialize Monitor
        console.print("[cyan]Step 2: Initializing Stock Monitor...[/cyan]")
        sm = StockMonitor(
            api=api,
            config_path=str(ROOT / "config" / "stocks.yaml"),
            dry_run=True  # Ensure no real orders are placed
        )
        
        target_strat = strategy_override or sm.strat_name
        console.print(f"Target Strategy: {target_strat}")
        
        # 3. Analyze Market Regime
        console.print("[cyan]Step 3: Checking Market Regime...[/cyan]")
        sm._update_market_regime()
        regime_tag = "BEAR" if sm.is_bear_market else "BULL"
        console.print(f"[green]✅ Current Market Regime: {regime_tag}[/green]")
        
        # 4. Check Watchlist and Data
        console.print(f"[cyan]Step 4: Checking data for first 5 watchlist items...[/cyan]")
        test_watchlist = sm.watchlist[:5]
        console.print(f"Watchlist head: {test_watchlist}")
        
        from strategies.stocks.entry_strategies import STOCK_STRATEGIES
        from strategies.stocks.multi_timeframe import analyze_market_condition, should_trade_based_on_tf
        
        if target_strat not in STOCK_STRATEGIES:
            console.print(f"[red]❌ Strategy {target_strat} not found in STOCK_STRATEGIES[/red]")
            return

        strat_func = STOCK_STRATEGIES[target_strat]["func"]

        for ticker in test_watchlist:
            console.print(f"\n[bold yellow]Analyzing {ticker}...[/bold yellow]")
            try:
                contract = api.Contracts.Stocks[ticker]
                now = pd.Timestamp.now()
                start_date = (now - pd.Timedelta(days=5)).strftime("%Y-%m-%d")
                end_date = now.strftime("%Y-%m-%d")
                
                kbars = api.kbars(contract, start=start_date, end=end_date)
                df = pd.DataFrame({**kbars})
                
                if df.empty:
                    console.print(f"[red]❌ No kbar data returned for {ticker}[/red]")
                    continue
                
                df.ts = pd.to_datetime(df.ts)
                df = df.set_index("ts")
                
                # Resample to 5m
                df_5m = df.resample('5min').agg({
                    'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last', 
                    'Volume': 'sum'
                }).dropna()
                
                # Basic indicators for strategy support
                df_5m['ma20'] = df_5m['Close'].rolling(20).mean()
                df_5m['ma60'] = df_5m['Close'].rolling(60).mean()
                df_5m['std20'] = df_5m['Close'].rolling(20).std()
                df_5m['bb_lower'] = df_5m['ma20'] - 2 * df_5m['std20']
                
                # Squeeze indicators (required for scout)
                from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
                df_5m = calculate_stock_squeeze(df_5m)
                
                last = df_5m.iloc[-1]
                console.print(f"Latest Price: {last['Close']}, BB Lower: {last.get('bb_lower', 0):.2f}")
                
                # Check MTF
                console.print("Running Multi-Timeframe Analysis...")
                mtf_res = analyze_market_condition(df_5m)
                should_trade, tf_analysis = should_trade_based_on_tf(df_5m)
                
                console.print(f"Primary Trend (60m): {mtf_res['market_state']['primary_trend']}")
                console.print(f"Market Regime: {mtf_res['market_state']['market_regime']}")
                console.print(f"Should Trade (MTF Filter): {'✅ YES' if should_trade else '❌ NO'}")
                if not should_trade:
                    console.print(f"MTF Reason: {mtf_res['trading_recommendation'].get('reason')}")
                    console.print(f"Filters Passed: {mtf_res['trading_recommendation'].get('filters_passed')}/4")
                
                # Check Strategy
                state = {
                    "df_5m": df_5m,
                    "last_5m": last,
                    "scout_stage": "IDLE",
                    "market_trend": regime_tag
                }
                res = strat_func(state, sm.cfg)
                if res:
                    console.print(f"[bold green]🎯 STRATEGY SIGNAL: {res['action']} - {res['reason']}[/bold green]")
                else:
                    console.print(f"[dim]⚪ No strategy signal for {target_strat}[/dim]")
                
            except Exception as e:
                console.print(f"[red]Error analyzing {ticker}: {e}[/red]")

    except Exception as e:
        console.print(f"[bold red]Diagnosis Failed: {e}[/bold red]")
        import traceback
        console.print(traceback.format_exc())
    finally:
        if api:
            logout()

if __name__ == "__main__":
    diagnose()
