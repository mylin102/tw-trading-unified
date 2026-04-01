#!/usr/bin/env python3
"""
tw-trading-unified — single Shioaji session, dual strategy.
Watches for restart flag from dashboard config changes.
"""
import sys
import os
import time
import argparse
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console
from core.shioaji_session import get_api, logout

console = Console()

BASE = os.path.dirname(__file__)
RESTART_FLAG = Path(BASE) / ".restart"


def tick_dispatcher(futures_mon, options_mon):
    def on_tick(exchange, tick):
        try:
            futures_mon.on_tick(exchange, tick)
        except Exception as e:
            console.print(f"[red][futures tick err] {e}[/red]")
        try:
            options_mon.on_tick(exchange, tick)
        except Exception as e:
            console.print(f"[red][options tick err] {e}[/red]")
    return on_tick


def run_once(dry_run=False):
    """Start both monitors. Returns True if restart requested."""
    api = None
    try:
        if not dry_run:
            api = get_api()
            console.print("[green]✅ Single Shioaji session established[/green]")
        else:
            console.print("[yellow]🔧 Dry-run — no broker login[/yellow]")

        from strategies.futures.monitor import FuturesMonitor
        fm = FuturesMonitor(
            api=api,
            config_path=os.path.join(BASE, "config", "futures.yaml"),
            dry_run=dry_run,
        )
        fm.setup()

        from strategies.options.monitor import OptionsMonitor
        om = OptionsMonitor(api=api, dry_run=dry_run)

        if api is not None:
            api.quote.set_on_tick_fop_v1_callback(tick_dispatcher(fm, om))

        ft = threading.Thread(target=fm.run, name="futures", daemon=True)
        ot = threading.Thread(target=om.run, name="options", daemon=True)
        ft.start()
        ot.start()
        console.print("[bold green]🚀 Both monitors running[/bold green]")

        # Watch for restart flag
        while ft.is_alive() or ot.is_alive():
            if RESTART_FLAG.exists():
                RESTART_FLAG.unlink()
                console.print("[bold yellow]🔄 Restart requested — shutting down...[/bold yellow]")
                fm.stop()
                om.stop()
                ft.join(timeout=5)
                ot.join(timeout=5)
                return True
            time.sleep(2)
        return False

    finally:
        logout()
        console.print("[green]Session logged out[/green]")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip broker login entirely")
    args = parser.parse_args()

    while True:
        restart = run_once(dry_run=args.dry_run)
        if not restart:
            break
        console.print("[bold cyan]⏳ Waiting 30s for session release...[/bold cyan]")
        time.sleep(30)
        console.print("[bold cyan]🔄 Restarting...[/bold cyan]")


if __name__ == "__main__":
    main()
