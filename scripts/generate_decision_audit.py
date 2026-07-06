import os
import sys
import logging
from datetime import datetime
from rich.console import Console
from rich.table import Table

# Ensure project root is in path
sys.path.insert(0, os.getcwd())
sys.path.insert(0, os.path.join(os.getcwd(), "strategies/futures"))

from squeeze_futures.database.db_manager import DatabaseManager
from core.performance.performance_aggregator import PerformanceAggregator

console = Console()

def generate_decision_audit():
    console.print("[bold cyan]Generating Decision Validation Audit...[/bold cyan]")
    
    db_path = "strategies/logs/trading_MXF.db"
    if not os.path.exists(db_path):
        console.print(f"[red]Database not found at {db_path}.[/red]")
        return

    db = DatabaseManager(db_path)
    
    # Fetch recent trades with their metadata
    trades = db.get_trades(limit=50)
    if not trades:
        console.print("[yellow]No recent trades found for auditing.[/yellow]")
        return

    # Header
    audit_md = "# 🛡️ Decision Validation Audit Report\n\n"
    audit_md += f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n"
    
    audit_md += "## Recent Trade Decisions\n"
    audit_md += "| Time | Strategy | Action | PnL | Reason/Context |\n"
    audit_md += "| :--- | :--- | :--- | :--- | :--- |\n"
    
    for t in trades:
        # Filter for completed trades (exits) to see the full story
        if t.get("type") in ("EXIT", "PARTIAL_EXIT"):
            time_str = t.get("exit_time", "")
            strat = t.get("exit_reason", "UNKNOWN").split(":")[0] # Simplified strat extraction
            action = f"{t.get('direction')} EXIT"
            pnl = f"{t.get('pnl_cash', 0):+,.0f}"
            reason = t.get("exit_reason", "")
            audit_md += f"| {time_str} | {strat} | {action} | {pnl} | {reason} |\n"

    # Add KillSwitch status section
    audit_md += "\n## ⚡ KillSwitch Status\n"
    aggregator = PerformanceAggregator(db)
    
    strategies = ["trend_continuation_v1", "adaptive_orb_v15", "counter_vwap"]
    audit_md += "| Strategy | Status | Reason |\n"
    audit_md += "| :--- | :--- | :--- |\n"
    
    from core.risk.kill_switch import KillSwitch
    ks = KillSwitch(aggregator, {}) # Default config for checking
    
    for s in strategies:
        allowed, reason = ks.is_strategy_allowed(s)
        status = "✅ ALLOWED" if allowed else "🛑 BLOCKED"
        audit_md += f"| {s} | {status} | {reason} |\n"

    # Save to file
    audit_path = "reports/decision_audit_report.md"
    os.makedirs("reports", exist_ok=True)
    with open(audit_path, "w") as f:
        f.write(audit_md)
    
    console.print(f"[green]✅ Audit report generated: {audit_path}[/green]")

if __name__ == "__main__":
    generate_decision_audit()
