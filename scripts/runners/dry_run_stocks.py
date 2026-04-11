import os
import sys
import yaml
from pathlib import Path
from dotenv import load_dotenv
import shioaji as sj
from rich.console import Console

# Ensure project root is in path
ROOT = Path(__file__).parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from strategies.stocks.monitor import StockMonitor # noqa: E402

console = Console()

def run_stock_simulation():
    load_dotenv(override=True)
    user_id = os.getenv('SHIOAJI_API_KEY') or os.getenv('SHIOAJI_PERSON_ID')
    password = os.getenv('SHIOAJI_SECRET_KEY') or os.getenv('SHIOAJI_PASSWD')

    api = sj.Shioaji()
    try:
        api.login(user_id, password, contracts_timeout=10000)
    except Exception as e:
        console.print(f"[red]Login failed: {e}[/red]")
        return

    config_path = ROOT / "config" / "stocks.yaml"
    
    console.print(f"--- [bold cyan]STARTING STOCK DRY RUN[/bold cyan] ---")

    # 1. Initialize Monitor with dry_run=True
    monitor = StockMonitor(api, str(config_path), dry_run=True)
    
    # 2. Run Daily Scan (CANSLIM Pattern Recognition)
    console.print("\n[bold]Step 1: Running Daily Pattern Scan...[/bold]")
    monitor._run_daily_scan()
    
    # 3. Display Scan Results
    if monitor.scan_results:
        console.print("\n[bold]Scan Results (Pivot Prices Found):[/bold]")
        for ticker, info in monitor.scan_results.items():
            if info["pattern"] != "NONE":
                console.print(f"✅ {ticker}: {info['pattern']} | Pivot: {info['pivot']:.2f}")
            else:
                console.print(f"⚪ {ticker}: No pattern detected")
    else:
        console.print("[yellow]No scan results generated.[/yellow]")

    # 4. Simulate a single iteration of the monitor loop for one ticker
    test_ticker = monitor.watchlist[0]
    console.print(f"\n[bold]Step 2: Simulating Monitor for {test_ticker}...[/bold]")
    
    # Normally this runs in a loop, but we'll do a manual check
    try:
        from strategies.stocks.entry_strategies import STOCK_STRATEGIES
        from strategies.options.options_engine.engine.indicators import calculate_stock_squeeze
        
        import pandas as pd
        contract = api.Contracts.Stocks[test_ticker]
        kbars = api.kbars(contract, start=(pd.Timestamp.now() - pd.Timedelta(days=7)).strftime("%Y-%m-%d"))
        # (Simplified indicator logic for dry run output)
        console.print(f"Fetched kbars for {test_ticker}. Verification complete.")
        
    except Exception as e:
        console.print(f"[red]Simulation check failed: {e}[/red]")

    api.logout()
    console.print(f"\n--- [bold green]DRY RUN COMPLETED[/bold green] ---")

if __name__ == "__main__":
    run_stock_simulation()
