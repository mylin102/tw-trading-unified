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
RESTART_RETRY_LIMIT = 5
RESTART_WINDOW_SECS = 300  # 5 minutes


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
    """Quick check if Shioaji session is still usable, with a small retry."""
    if api is None:
        return False
    for _ in range(2): # 兩次機會
        try:
            api.list_positions(api.futopt_account)
            return True
        except Exception:
            time.sleep(1)
    return False


def run_system(dry_run=False):
    """運行交易系統，遇到斷線或重啟請求時結束進程，由外部腳本重新拉起"""
    # 啟動時立即清除重啟旗標，避免循環重啟
    if RESTART_FLAG.exists():
        RESTART_FLAG.unlink()
        console.print("[dim]Old restart flag cleared.[/dim]")

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

        from strategies.stocks.monitor import StockMonitor
        sm = StockMonitor(
            api=api,
            config_path=os.path.join(BASE, "config", "stocks.yaml"),
            dry_run=dry_run
        )

        # 先初始化 contracts，再訂閱
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

            # Subscribe options
            for key in ["MTX", "C", "P"]:
                con = om.monitor.active_contracts.get(key)
                if con:
                    api.quote.subscribe(con, quote_type='tick')
                    api.quote.subscribe(con, quote_type=sj.constant.QuoteType.BidAsk)
                    console.print(f"[green]📡 Subscribed {key}: {con.code} (tick+bidask)[/green]")

        ft = threading.Thread(target=fm.run, name="futures", daemon=True)
        ot = threading.Thread(target=om.run, name="options", daemon=True)
        st_t = threading.Thread(target=sm.run, name="stocks", daemon=True)
        
        ft.start()
        ot.start()
        st_t.start()
        console.print("[bold green]🚀 All monitors running (Futures, Options, Stocks)[/bold green]")

        startup_grace_until = time.time() + 60
        health_check_at = time.time() + HEALTH_INTERVAL

        while ft.is_alive() and ot.is_alive() and st_t.is_alive():
            if RESTART_FLAG.exists():
                RESTART_FLAG.unlink()
                console.print("[bold yellow]🔄 Restart requested. Exiting for external supervisor...[/bold yellow]")
                break

            now = time.time()
            if not dry_run and now > startup_grace_until and now > health_check_at:
                if not api_is_healthy(api):
                    console.print("[red]💀 Shioaji session dead — exiting for external supervisor[/red]")
                    break
                health_check_at = now + HEALTH_INTERVAL
            time.sleep(2)

    except Exception as exc:
        console.print(f"[bold red]Critical crash: {exc}[/bold red]")
    finally:
        # 關閉流程
        console.print("[dim]Stopping monitors and threads...[/dim]")
        try:
            if 'fm' in locals():
                fm.stop()
            if 'om' in locals():
                om.stop()
            if 'sm' in locals():
                sm.stop()
            
            # 給予執行緒時間結束
            if 'ft' in locals():
                ft.join(timeout=5)
            if 'ot' in locals():
                ot.join(timeout=5)
            if 'st_t' in locals():
                st_t.join(timeout=5)
            
            if api is not None:
                api.quote.set_on_tick_fop_v1_callback(lambda ex, t: None)
                api.quote.set_on_bidask_fop_v1_callback(lambda ex, b: None)
            
            logout()
            console.print("[green]Session logged out cleanly. Exiting...[/green]")
        except Exception as e:
            console.print(f"[dim]Cleanup error: {e}[/dim]")
    
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip broker login entirely")
    args = parser.parse_args()
    try:
        run_system(dry_run=args.dry_run)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted by user[/yellow]")
        sys.exit(0)
    except Exception as e:
        console.print(f"[bold red]Unhandled exception in main: {e}[/bold red]")
        sys.exit(1)

if __name__ == "__main__":
    main()
