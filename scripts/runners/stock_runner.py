#!/usr/bin/env python3
"""
Independent Stock Trading Runner
Purpose: Isolate Stock module from Futures/Options to prevent cascading failures.
Rational: Independent login session, simplified monitoring, and auto-restart capability.
"""
import sys
import os
import time
import signal
import fcntl
from pathlib import Path
from rich.console import Console

# Add project root to path
BASE = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE))

from core.shioaji_session import get_api, logout
from strategies.stocks.monitor import StockMonitor

console = Console()

# ── GSD: Singleton Lock ─────────────────────────────────────────────
LOCKFILE = "/tmp/stock_runner_singleton.lock"

def check_singleton():
    """Ensure only ONE stock_runner is running using a fixed lockfile."""
    try:
        # Create lockfile if it doesn't exist
        lock_fd = open(LOCKFILE, 'w')
        # Try to acquire an exclusive lock
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Write PID to lockfile
        lock_fd.seek(0)
        lock_fd.write(str(os.getpid()))
        lock_fd.truncate()
        return lock_fd
    except (IOError, OSError):
        # Could not acquire lock, another process is running
        try:
            with open(LOCKFILE, 'r') as f:
                existing_pid = f.read().strip()
            console.print(f"[bold red]🔒 Another stock_runner (PID={existing_pid}) is already running. Exiting.[/bold red]")
        except:
            console.print("[bold red]🔒 Another stock_runner is already running. Exiting.[/bold red]")
        sys.exit(1)

def run_stock_monitor(dry_run=False):
    api = None
    lock_fd = check_singleton()

    try:
        console.print("[bold green]🍎 Starting Standalone Stock Monitor...[/bold green]")

        # 1. Login
        api = get_api()
        console.print("[green]✅ Shioaji session established for Stocks[/green]")

        # 2. Initialize Monitor
        sm = StockMonitor(
            api=api,
            config_path=os.path.join(BASE, "config", "stocks.yaml"),
            dry_run=dry_run
        )
        
        # [GSD Fix] 啟動時執行標準設置（包含恢復部位與保存狀態）
        sm.setup()
        sm._save_orders_file()

        # 3. Execution Loop
        RESTART_FLAG = BASE / ".restart"
        
        while True:
            # Check for restart flag from dashboard
            if RESTART_FLAG.exists():
                console.print("[bold yellow]🔄 Restart flag detected. Exiting for supervisor...[/bold yellow]")
                break
                
            try:
                sm.run_iteration()
            except Exception as e:
                console.print(f"[bold red]Error in run_iteration: {e}[/bold red]")
                import traceback
                console.print(traceback.format_exc())
            time.sleep(1)

    except KeyboardInterrupt:
        console.print("[yellow]Stopping Stock Monitor (User Interrupt)...[/yellow]")
    except Exception as e:
        console.print(f"[bold red]Stock Runner Crash: {e}[/bold red]")
        import traceback
        console.print(traceback.format_exc())
    finally:
        if api:
            logout()
        # Release lock
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            os.unlink(LOCKFILE)
        except Exception:
            pass
        console.print("[dim]Stock Runner finished.[/dim]")

if __name__ == "__main__":
    # Standard graceful shutdown
    signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda s, f: sys.exit(0))

    dry_run = "--dry-run" in sys.argv or "--dry" in sys.argv
    if dry_run:
        console.print("[bold yellow]🧪 DRY RUN MODE — 不下單、不寫交易紀錄[/bold yellow]")
    run_stock_monitor(dry_run=dry_run)
