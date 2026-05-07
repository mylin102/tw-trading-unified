import os
import sys
import pandas as pd
from datetime import datetime, timedelta
from rich.console import Console
from rich.table import Table

# Ensure project root is in path
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "strategies/futures"))

from squeeze_futures.database.db_manager import DatabaseManager
from core.shioaji_session import ShioajiSession

console = Console()

def validate_pnl_consistency():
    console.print("[bold yellow]Cross-Checking PnL Consistency: SQLite vs Broker[/bold yellow]")
    
    # 1. Fetch SQLite PnL
    db_path = "strategies/logs/trading_MXF.db"
    if not os.path.exists(db_path):
        console.print("[red]Database not found.[/red]")
        return
        
    db = DatabaseManager(db_path)
    local_trades = db.get_trade_history()
    local_pnl = sum(t.get("pnl_cash", 0) for t in local_trades if t.get("type") in ("EXIT", "PARTIAL_EXIT"))
    
    console.print(f"[cyan]Local SQLite Total PnL: {local_pnl:+,.0f} TWD[/cyan]")

    # 2. Fetch Broker PnL (requires API session)
    # This part is interactive/env-dependent, we'll try to use the session singleton
    try:
        session = ShioajiSession()
        api = session.get_api()
        if not api:
             console.print("[yellow]Skipping Broker check: API session not active.[/yellow]")
        else:
            # Check last 30 days
            start = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            end = datetime.now().strftime("%Y-%m-%d")
            
            pnl_list = api.list_profit_loss(start_date=start, end_date=end)
            broker_pnl = sum(float(p.pnl) for p in pnl_list)
            
            diff = local_pnl - broker_pnl
            
            table = Table(title="PnL Consistency Check")
            table.add_column("Source", style="bold")
            table.add_column("PnL (TWD)", justify="right")
            
            table.add_row("SQLite (Local)", f"{local_pnl:+,.0f}")
            table.add_row("Shioaji (Broker)", f"{broker_pnl:+,.0f}")
            table.add_row("Discrepancy", f"{diff:+,.0f}", style="bold red" if abs(diff) > 10 else "green")
            
            console.print(table)
            
            if abs(diff) > 100:
                console.print("[bold red]🚨 CRITICAL DISCREPANCY DETECTED! L4 decisions may be unreliable.[/bold red]")
            else:
                console.print("[bold green]✅ PnL Consistency validated within tolerance.[/bold green]")

    except Exception as e:
        console.print(f"[red]Consistency check failed: {e}[/red]")

if __name__ == "__main__":
    validate_pnl_consistency()
