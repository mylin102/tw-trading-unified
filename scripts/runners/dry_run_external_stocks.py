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
from core.external_feature_provider import get_external_feature_provider, load_stock_config  # noqa: E402

console = Console()

def fetch_external_watchlist():
    cfg = load_stock_config(ROOT / "config" / "stocks.yaml")
    provider = get_external_feature_provider(cfg)
    console.print("[cyan]🌐 Fetching external watchlist via external feature provider...[/cyan]")
    try:
        snapshot = provider.get_snapshot(prefer_refresh=True)
        symbols = snapshot.get("watchlist_symbols", [])
        if snapshot.get("degraded"):
            console.print(f"[yellow]⚠️ Using degraded feature snapshot: {snapshot.get('degraded_reason', '')}[/yellow]")
        console.print(f"[green]✅ Successfully fetched {len(symbols)} stocks from external source.[/green]")
        return symbols
    except Exception as e:
        console.print(f"[red]❌ Failed to fetch external watchlist: {e}[/red]")
        return None

def run_external_stock_simulation():
    load_dotenv(override=True)
    user_id = os.getenv('SHIOAJI_API_KEY') or os.getenv('SHIOAJI_PERSON_ID')
    password = os.getenv('SHIOAJI_SECRET_KEY') or os.getenv('SHIOAJI_PASSWD')

    if not user_id or not password:
        console.print("[red]SHIOAJI credentials not found in .env[/red]")
        return

    # 1. Fetch external list
    new_watchlist = fetch_external_watchlist()
    if not new_watchlist:
        return

    api = sj.Shioaji()
    try:
        api.login(user_id, password, contracts_timeout=10000)
    except Exception as e:
        console.print(f"[red]Login failed: {e}[/red]")
        return

    # 2. Prepare temporary config
    config_path = ROOT / "config" / "stocks.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    
    # Override watchlist
    cfg["stocks"]["watchlist"] = new_watchlist
    
    temp_config_path = ROOT / "config" / "stocks_dry_run_ext.yaml"
    with open(temp_config_path, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, allow_unicode=True, default_flow_style=False)

    console.print(f"--- [bold cyan]STARTING EXTERNAL STOCK DRY RUN[/bold cyan] ---")
    console.print(f"Watchlist: {new_watchlist[:10]}... ({len(new_watchlist)} total)")

    # 3. Initialize Monitor with dry_run=True and temporary config
    monitor = StockMonitor(api, str(temp_config_path), dry_run=True)
    
    # 4. Run Daily Scan (CANSLIM Pattern Recognition)
    console.print("\n[bold]Step 1: Running Daily Pattern Scan...[/bold]")
    monitor._run_daily_scan()
    
    # 5. Display Scan Results
    if monitor.scan_results:
        console.print("\n[bold]Scan Results (Pivot Prices Found):[/bold]")
        count = 0
        for ticker, info in monitor.scan_results.items():
            if info["pattern"] != "NONE":
                console.print(f"✅ {ticker}: {info['pattern']} | Pivot: {info['pivot']:.2f}")
                count += 1
            if count >= 10: break
        
        if count == 0:
            console.print("[yellow]No patterns detected in current market state for the watchlist.[/yellow]")
    else:
        console.print("[yellow]No scan results generated.[/yellow]")

    # 6. Clean up
    if temp_config_path.exists():
        os.remove(temp_config_path)

    api.logout()
    console.print(f"\n--- [bold green]DRY RUN COMPLETED[/bold green] ---")

if __name__ == "__main__":
    run_external_stock_simulation()
