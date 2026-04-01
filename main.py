#!/usr/bin/env python3
"""
tw-trading-unified — single Shioaji session, dual strategy.
Watches for restart flag from dashboard config changes.
On restart: kills entire process and re-execs for a clean Shioaji session.
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
HEALTH_INTERVAL = 30  # seconds between health checks


def tick_dispatcher(futures_mon, options_mon):
    _seen_codes = set()
    def on_tick(exchange, tick):
        if tick.code not in _seen_codes:
            _seen_codes.add(tick.code)
            console.print(f"[cyan]📥 New tick code: {tick.code} close={tick.close}[/cyan]")
        try:
            futures_mon.on_tick(exchange, tick)
        except Exception as e:
            console.print(f"[red][futures tick err] {e}[/red]")
        try:
            options_mon.on_tick(exchange, tick)
        except Exception as e:
            console.print(f"[red][options tick err] {e}[/red]")
    return on_tick


def bidask_dispatcher(options_mon):
    """Route BidAsk updates to options monitor for IV calculation."""
    _seen = set()
    def on_bidask(exchange, bidask):
        if bidask.code not in _seen:
            _seen.add(bidask.code)
            bid = bidask.bid_price[0] if hasattr(bidask.bid_price, '__getitem__') else bidask.bid_price
            ask = bidask.ask_price[0] if hasattr(bidask.ask_price, '__getitem__') else bidask.ask_price
            console.print(f"[cyan]📥 New bidask: {bidask.code} bid={bid} ask={ask}[/cyan]")
        # Direct update: bypass on_bidask method, write to monitor's market_data directly
        mon = options_mon.monitor if hasattr(options_mon, 'monitor') else options_mon
        code = bidask.code
        bid = bidask.bid_price[0] if hasattr(bidask.bid_price, '__getitem__') else float(bidask.bid_price)
        ask = bidask.ask_price[0] if hasattr(bidask.ask_price, '__getitem__') else float(bidask.ask_price)
        if bid <= 0 or ask <= 0:
            return
        mid = (bid + ask) / 2
        # Match by code
        matched = False
        for key in ["C", "P", "MTX"]:
            con = mon.active_contracts.get(key)
            if con and (code == getattr(con, "code", None) or (key == "MTX" and code.startswith("MXF"))):
                mon.market_data[key]["bid"] = float(bid)
                mon.market_data[key]["ask"] = float(ask)
                if mon.market_data[key]["close"] <= 0 or key == "MTX":
                    mon.market_data[key]["close"] = mid
                matched = True
                if code.startswith("MXF"):
                    console.print(f"[green]✅ MTX updated: {mon.market_data['MTX']['close']:.0f}[/green]")
                break
        if not matched and code not in _seen:
            console.print(f"[yellow]bidask unmatched: {code}, contracts={list(mon.active_contracts.keys())}[/yellow]")
    return on_bidask


def api_is_healthy(api):
    """Quick check if Shioaji session is still usable."""
    if api is None:
        return False
    try:
        api.list_positions(api.futopt_account)
        return True
    except Exception:
        return False


def run(dry_run=False):
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

        # 先初始化 contracts，再訂閱（避免 race condition）
        om.monitor.find_best_contracts()
        om.monitor.pre_fill_bars()

        if api is not None:
            import shioaji as sj
            api.quote.set_on_tick_fop_v1_callback(tick_dispatcher(fm, om))
            api.quote.set_on_bidask_fop_v1_callback(bidask_dispatcher(om))

            # Subscribe TMF tick
            if fm.contract is not None:
                api.quote.subscribe(fm.contract, quote_type='tick')
                console.print(f"[green]📡 Subscribed TMF tick: {fm.contract.code}[/green]")

            # Subscribe options tick + bidask (contracts already initialized)
            for key in ["MTX", "C", "P"]:
                con = om.monitor.active_contracts.get(key)
                if con:
                    api.quote.subscribe(con, quote_type='tick')
                    api.quote.subscribe(con, quote_type=sj.constant.QuoteType.BidAsk)
                    console.print(f"[green]📡 Subscribed {key}: {con.code} (tick+bidask)[/green]")

        ft = threading.Thread(target=fm.run, name="futures", daemon=True)
        ot = threading.Thread(target=om.run, name="options", daemon=True)
        ft.start()
        ot.start()
        console.print("[bold green]🚀 Both monitors running[/bold green]")

        health_check_at = time.time() + HEALTH_INTERVAL

        while ft.is_alive() and ot.is_alive():
            if RESTART_FLAG.exists():
                RESTART_FLAG.unlink()
                console.print("[bold yellow]🔄 Restart requested[/bold yellow]")
                break

            # Periodic health check
            if not dry_run and time.time() > health_check_at:
                if not api_is_healthy(api):
                    console.print("[red]💀 Shioaji session dead — restarting process[/red]")
                    break
                health_check_at = time.time() + HEALTH_INTERVAL

            time.sleep(2)

        if not ft.is_alive() or not ot.is_alive():
            dead = []
            if not ft.is_alive(): dead.append("futures")
            if not ot.is_alive(): dead.append("options")
            console.print(f"[red]💀 Thread died: {', '.join(dead)}[/red]")

        # Stop monitors
        fm.stop()
        om.stop()
        ft.join(timeout=5)
        ot.join(timeout=5)

    finally:
        try:
            logout()
            console.print("[green]Session logged out[/green]")
        except Exception:
            pass

    # Re-exec entire process for a completely clean Shioaji session
    console.print("[bold cyan]🔄 Re-executing process in 10s...[/bold cyan]")
    time.sleep(10)
    os.execv(sys.executable, [sys.executable] + sys.argv)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip broker login entirely")
    args = parser.parse_args()
    run(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
