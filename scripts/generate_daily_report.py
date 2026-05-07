import os
import sys
from datetime import datetime
import pandas as pd
from rich.console import Console
from rich.table import Table

# Ensure project root is in path
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "strategies/futures"))

from squeeze_futures.database.db_manager import DatabaseManager
from core.performance.performance_aggregator import PerformanceAggregator

console = Console()

def generate_daily_report():
    console.print("[bold blue]Generating Daily Performance Report...[/bold blue]")
    
    db_path = "strategies/logs/trading_MXF.db"
    if not os.path.exists(db_path):
        console.print(f"[red]Database not found at {db_path}. No data to report.[/red]")
        return

    db = DatabaseManager(db_path)
    aggregator = PerformanceAggregator(db)
    
    today = datetime.now().strftime("%Y-%m-%d")
    metrics = aggregator.get_daily_metrics(today)
    
    # Header
    report_md = f"# 📊 Daily Trading Report - {today}\n\n"
    
    if not metrics:
        report_md += "No trades executed today.\n"
    else:
        report_md += f"## Summary\n"
        report_md += f"- **Trades**: {metrics['count']}\n"
        report_md += f"- **Win Rate**: {metrics['win_rate']}\n"
        report_md += f"- **Net PnL**: {metrics['net_pnl_cash']} TWD ({metrics['net_pnl_pts']} pts)\n"
        report_md += f"- **Avg PnL**: {metrics['avg_pnl']} TWD\n"
        report_md += f"- **Max Drawdown**: {metrics['max_drawdown']} TWD\n\n"

    # Strategy Ranking (Mock data for now, will be real once strategy names are in DB)
    report_md += "## Strategy Ranking\n"
    report_md += "| Strategy | Trades | Win Rate | Net PnL |\n"
    report_md += "| :--- | :--- | :--- | :--- |\n"
    report_md += "| trend_continuation_v1 | - | - | - |\n"
    report_md += "| adaptive_orb_v15 | - | - | - |\n"
    
    # Save to file
    report_dir = "reports"
    os.makedirs(report_dir, exist_ok=True)
    report_path = os.path.join(report_dir, f"daily_report_{today}.md")
    
    with open(report_path, "w") as f:
        f.write(report_md)
    
    console.print(f"[green]✅ Report generated: {report_path}[/green]")
    console.print(report_md)

if __name__ == "__main__":
    generate_daily_report()
