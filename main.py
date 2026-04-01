#!/usr/bin/env python3
"""
tw-trading-unified — single Shioaji session, dual strategy.
"""
import sys
import os
import argparse
import threading

sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console
from core.shioaji_session import get_api, logout

console = Console()

BASE = os.path.dirname(__file__)


def tick_dispatcher(futures_mon, options_mon):
    """Return a callback that fans out ticks to both monitors."""
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Both strategies paper mode, no broker login")
    parser.add_argument("--futures-paper", action="store_true", help="Force futures to paper mode")
    parser.add_argument("--options-paper", action="store_true", help="Force options to paper mode")
    args = parser.parse_args()

    all_dry = args.dry_run
    futures_dry = all_dry or args.futures_paper
    options_dry = all_dry or args.options_paper

    api = None
    try:
        need_login = not (futures_dry and options_dry)
        if need_login:
            api = get_api()
            console.print("[green]✅ Single Shioaji session established[/green]")
        else:
            console.print("[yellow]🔧 Both strategies paper — no broker login[/yellow]")

        # --- Futures monitor ---
        from strategies.futures.monitor import FuturesMonitor
        fm = FuturesMonitor(
            api=api,
            config_path=os.path.join(BASE, "config", "futures.yaml"),
            dry_run=futures_dry,
        )
        fm.setup()
        console.print(f"  futures: {'PAPER' if futures_dry else 'LIVE'}")

        # --- Options monitor ---
        from strategies.options.monitor import OptionsMonitor
        om = OptionsMonitor(api=api, dry_run=options_dry)
        console.print(f"  options: {'PAPER' if options_dry else 'LIVE'}")

        # --- Tick subscription ---
        if api is not None:
            api.quote.set_on_tick_fop_v1_callback(tick_dispatcher(fm, om))

        # --- Run both in threads ---
        ft = threading.Thread(target=fm.run, name="futures", daemon=True)
        ot = threading.Thread(target=om.run, name="options", daemon=True)

        ft.start()
        ot.start()
        console.print("[bold green]🚀 Both monitors running[/bold green]")

        ft.join()
        ot.join()

    except KeyboardInterrupt:
        console.print("[yellow]Shutting down...[/yellow]")
    finally:
        logout()
        console.print("[green]Session logged out[/green]")


if __name__ == "__main__":
    main()
