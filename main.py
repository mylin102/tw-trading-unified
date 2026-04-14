#!/usr/bin/env python3
"""
tw-trading-unified — single Shioaji session, dual strategy.
Watches for restart flag from dashboard config changes.
On restart: kills entire process and re-execs for a clean Shioaji session.
"""
import sys
import os
import time
import signal
import argparse
import threading
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))

from rich.console import Console
from core.shioaji_session import get_api, logout

console = Console()

# [P0 Fix] Connection state tracking via Shioaji event callback
_connection_dropped = False

BASE = os.path.dirname(__file__)
RESTART_FLAG = Path(BASE) / ".restart"
HEALTH_INTERVAL = 30  # seconds between health checks
RESTART_RETRY_LIMIT = 5
RESTART_WINDOW_SECS = 300  # 5 minutes

# macOS graceful shutdown flag
_shutdown_event = threading.Event()


def tick_dispatcher(futures_mon, options_mon):
    """Safely dispatch futures and options ticks with shutdown protection."""
    _seen_codes = set()
    _lock = threading.Lock()
    
    def on_tick(exchange, tick):
        # Safety checks
        if _shutdown_event.is_set():
            return
        if tick is None or not hasattr(tick, 'code'):
            return
        
        try:
            with _lock:
                if tick.code not in _seen_codes:
                    _seen_codes.add(tick.code)
                    console.print(f"[cyan]📥 New tick code: {tick.code} close={tick.close}[/cyan]")
        except Exception as e:
            console.print(f"[red][tick tracking err] {e}[/red]")
        
        try:
            futures_mon.on_tick(exchange, tick)
        except Exception as e:
            console.print(f"[red][futures tick err] {e}[/red]")
        
        try:
            options_mon.on_tick(exchange, tick)
        except Exception as e:
            console.print(f"[red][options tick err] {e}[/red]")
    
    return on_tick


def bidask_dispatcher(futures_mon, options_mon):
    """Route BidAsk updates to monitors for IV calculation and data freshness."""
    _seen = set()
    _lock = threading.Lock()
    
    def on_bidask(exchange, bidask):
        # Safety checks
        if _shutdown_event.is_set():
            return
        if bidask is None or not hasattr(bidask, 'code'):
            return
        
        try:
            with _lock:
                if bidask.code not in _seen:
                    _seen.add(bidask.code)
                    bid = bidask.bid_price[0] if hasattr(bidask.bid_price, '__getitem__') else bidask.bid_price
                    ask = bidask.ask_price[0] if hasattr(bidask.ask_price, '__getitem__') else bidask.ask_price
                    console.print(f"[cyan]📥 New bidask: {bidask.code} bid={bid} ask={ask}[/cyan]")
            
            # Use monitor instance (handle OptionsMonitor wrapper)
            mon = options_mon.monitor if hasattr(options_mon, 'monitor') else options_mon
            code = bidask.code
            
            # Safe price extraction
            try:
                bid = bidask.bid_price[0] if hasattr(bidask.bid_price, '__getitem__') else float(bidask.bid_price)
                ask = bidask.ask_price[0] if hasattr(bidask.ask_price, '__getitem__') else float(bidask.ask_price)
            except (ValueError, TypeError, IndexError):
                return
            
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
                    
                    # 💡 GSD: Update freshness timestamp to prevent watchdog from restarting
                    mon.last_tick_at = time.time()
                    if hasattr(futures_mon, 'last_tick_at'):
                        futures_mon.last_tick_at = time.time()
                    
                    # 💡 GSD: Also update FuturesMonitor's internal market price cache if it exists
                    if key == "MTX" and hasattr(futures_mon, 'market_data'):
                        futures_mon.market_data["MTX"]["close"] = mid
                    
                    matched = True
                    if code.startswith("MXF"):
                        console.print(f"[green]✅ MTX updated: {mon.market_data['MTX']['close']:.0f}[/green]")
                    break
            
            if not matched and code not in _seen:
                console.print(f"[yellow]bidask unmatched: {code}, contracts={list(mon.active_contracts.keys())}[/yellow]")
        except Exception as e:
            console.print(f"[red][bidask dispatch err] {e}[/red]")
    
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


def _setup_event_callback(api, fm, om):
    """[P0 Fix] Monitor Shioaji connection state via event callback.

    Event codes:
      12 = RECONNECTING_NOTICE (session dropped, reconnecting)
      13 = RECONNECTED_NOTICE (reconnected successfully)
      16 = SUBSCRIPTION_OK (subscription confirmed)
      20 = REPUBLISH_UNACKED (unknown publisher flow — needs resubscribe)
    """
    global _connection_dropped

    @api.quote.on_event
    def event_cb(resp_code, event_code, info, event):
        """
        resp_code: Response code (0=ok)
        event_code: 12=RECONNECTING, 13=RECONNECTED, 16=SUBSCRIPTION_OK, 20=GD_FAIL
        info: Info string
        event: Event description
        """
        global _connection_dropped

        if event_code == 12:
            console.print("[bold yellow]🔌 Shioaji 斷線！開始自動重連 (最多 50 次)...[/bold yellow]")
            _connection_dropped = True

        elif event_code == 13:
            console.print("[bold green]✅ Shioaji 重連成功！恢復資料流[/bold green]")
            _connection_dropped = False
            # Re-subscribe to ensure data flow restoration
            try:
                if fm and fm.contract:
                    api.quote.subscribe(fm.contract, quote_type='tick')
                    console.print(f"[dim]📡 Re-subscribed TMF: {fm.contract.code}[/dim]")
            except Exception as e:
                console.print(f"[red]⚠️ Re-subscribe TMF failed: {e}[/red]")

        elif event_code == 16:
            console.print("[dim]📡 Shioaji 訂閱成功確認[/dim]")

        elif event_code == 20:
            console.print("[bold red]❌ Shioaji GD flow 失敗 — unknown publisher flow，需重新訂閱所有 contract[/bold red]")
            _connection_dropped = True
            # Force resubscribe all
            try:
                if fm and fm.contract:
                    api.quote.subscribe(fm.contract, quote_type='tick')
                if om:
                    for key in ["MTX", "C", "P"]:
                        con = om.monitor.active_contracts.get(key)
                        if con:
                            api.quote.subscribe(con, quote_type='tick')
                            api.quote.subscribe(con, quote_type=sj.constant.QuoteType.BidAsk)
                console.print("[green]✅ 已完成全部 contract 重新訂閱[/green]")
            except Exception as e:
                console.print(f"[red]❌ 重新訂閱失敗: {e}[/red]")

        else:
            # Log other events at debug level
            console.print(f"[dim]📋 Shioaji event: code={event_code}, event={event}[/dim]")


def _resubscribe_all(api, fm, om):
    """[P0 Fix] Helper to resubscribe all contracts after connection recovery."""
    import shioaji as sj
    try:
        if fm and fm.contract:
            api.quote.subscribe(fm.contract, quote_type='tick')
            console.print(f"[green]📡 Re-subscribed TMF: {fm.contract.code}[/green]")

        if om:
            for key in ["MTX", "C", "P"]:
                con = om.monitor.active_contracts.get(key)
                if con:
                    api.quote.subscribe(con, quote_type='tick')
                    api.quote.subscribe(con, quote_type=sj.constant.QuoteType.BidAsk)
                    console.print(f"[green]📡 Re-subscribed {key}: {con.code}[/green]")
    except Exception as e:
        console.print(f"[red]❌ Resubscribe failed: {e}[/red]")


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

        # [GSD] Session-aware config: night uses futures_night.yaml (wider stops, longer VWAP confirm)
        from core.date_utils import is_night_session
        from datetime import datetime as _dt
        _is_night = is_night_session(_dt.now())
        _config_file = "futures_night.yaml" if _is_night else "futures.yaml"
        console.print(f"[dim]📋 Futures config: {_config_file} (session={'night' if _is_night else 'day'})[/dim]")

        fm = FuturesMonitor(
            api=api,
            config_path=os.path.join(BASE, "config", _config_file),
            dry_run=dry_run,
        )
        fm.setup()

        from strategies.options.monitor import OptionsMonitor
        om = OptionsMonitor(api=api, dry_run=dry_run)

        # GSD Rationale: Stock module moved to scripts/stock_runner.py for fault isolation.
        # main.py now only handles Futures + Options which share the FOP callback session.

        # 先初始化 contracts，再訂閱
        om.monitor.find_best_contracts()
        om.monitor.pre_fill_bars()

        if api is not None:
            import shioaji as sj
            api.quote.set_on_tick_fop_v1_callback(tick_dispatcher(fm, om))
            api.quote.set_on_bidask_fop_v1_callback(bidask_dispatcher(fm, om))

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

            # [P0 Fix] Setup connection event monitoring
            _setup_event_callback(api, fm, om)
            console.print("[green]✅ Connection event callback registered[/green]")

        ft = threading.Thread(target=fm.run, name="futures", daemon=True)
        ot = threading.Thread(target=om.run, name="options", daemon=True)
        
        ft.start()
        ot.start()
        console.print("[bold green]🚀 Unified Monitors Running (Futures, Options)[/bold green]")

        startup_grace_until = time.time() + 60
        health_check_at = time.time() + HEALTH_INTERVAL

        # [gstack Sentinel] 數據新鮮度追蹤 — 二次確認防誤判
        last_data_at = time.time()
        stagnation_warned = False  # 第一次只警告，第二次才重啟
        max_restarts = 5  # Prevent infinite restart loop
        restart_count = 0

        while restart_count < max_restarts:
            # [Auto-Restart] Check if threads died unexpectedly
            if not ft.is_alive() or not ot.is_alive():
                dead = []
                if not ft.is_alive(): dead.append("futures")
                if not ot.is_alive(): dead.append("options")
                console.print(f"[bold red]💀 Thread died: {', '.join(dead)}. Restarting (attempt {restart_count+1}/{max_restarts})...[/bold red]")
                restart_count += 1

                # Re-initialize monitors and threads
                try:
                    from strategies.futures.monitor import FuturesMonitor
                    # [GSD] Session-aware config on restart too
                    from core.date_utils import is_night_session
                    from datetime import datetime as _dt2
                    _is_night = is_night_session(_dt2.now())
                    _config_file = "futures_night.yaml" if _is_night else "futures.yaml"
                    fm = FuturesMonitor(
                        api=api,
                        config_path=os.path.join(BASE, "config", _config_file),
                        dry_run=dry_run,
                    )
                    fm.setup()

                    from strategies.options.monitor import OptionsMonitor
                    om = OptionsMonitor(api=api, dry_run=dry_run)

                    # Re-subscribe
                    if api is not None:
                        import shioaji as sj
                        api.quote.set_on_tick_fop_v1_callback(tick_dispatcher(fm, om))
                        api.quote.set_on_bidask_fop_v1_callback(bidask_dispatcher(fm, om))

                        if fm.contract is not None:
                            api.quote.subscribe(fm.contract, quote_type='tick')

                        om.monitor.find_best_contracts()
                        om.monitor.pre_fill_bars()
                        for key in ["MTX", "C", "P"]:
                            con = om.monitor.active_contracts.get(key)
                            if con:
                                api.quote.subscribe(con, quote_type='tick')
                                api.quote.subscribe(con, quote_type=sj.constant.QuoteType.BidAsk)

                    ft = threading.Thread(target=fm.run, name="futures", daemon=True)
                    ot = threading.Thread(target=om.run, name="options", daemon=True)
                    ft.start()
                    ot.start()
                    last_data_at = time.time()  # Reset staleness timer
                    stagnation_warned = False
                    console.print(f"[bold green]✅ Restarted threads (attempt {restart_count}/{max_restarts})[/bold green]")
                    time.sleep(10)  # Grace period after restart
                    continue
                except Exception as e:
                    console.print(f"[bold red]💥 Restart failed: {e}[/bold red]")
                    import traceback
                    console.print(traceback.format_exc())
                    break

            now = time.time()
            
            # 檢查任何 FOP tick 是否有進來 (TMF 成交量低，單獨追蹤會誤判)
            fm_last = getattr(fm, 'last_tick_at', 0)
            om_last = getattr(om.monitor, 'last_tick_at', 0)
            latest_tick = max(fm_last, om_last)
            
            if latest_tick > last_data_at:
                last_data_at = latest_tick
                stagnation_warned = False  # tick 恢復，重置警告
            
            # 哨兵邏輯：二次確認 — 5 分鐘警告，10 分鐘才重啟（全天候監控，含夜盤）
            stale_secs = now - last_data_at
            if stale_secs > 600:
                console.print(f"[bold red]🚨 DATA STAGNATION CONFIRMED! No data for {stale_secs/60:.1f} mins (fm={now-fm_last:.1f}s, om={now-om_last:.1f}s ago). Force restarting...[/bold red]")
                break
            elif stale_secs > 300 and not stagnation_warned:
                console.print(f"[bold yellow]⚠️ DATA WARNING: No data for {stale_secs/60:.1f} mins. Watching...[/bold yellow]")
                stagnation_warned = True
            
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
        # Signal shutdown to all dispatchers
        _shutdown_event.set()
        
        # Closing sequence
        console.print("[dim]Stopping monitors and threads...[/dim]")
        try:
            if 'fm' in locals():
                fm.stop()
            if 'om' in locals():
                om.stop()
            if 'sm' in locals():
                sm.stop()

            # Give threads time to finish current operations
            time.sleep(1)

            # Join threads with timeout
            if 'ft' in locals():
                ft.join(timeout=5)
            if 'ot' in locals():
                ot.join(timeout=5)
            if 'st_t' in locals():
                st_t.join(timeout=5)

            # Clear callbacks before logout
            if api is not None:
                try:
                    api.quote.set_on_tick_fop_v1_callback(lambda ex, t: None)
                    time.sleep(0.5)  # Buffer for C++ callback cleanup
                    api.quote.set_on_bidask_fop_v1_callback(lambda ex, b: None)
                    time.sleep(0.5)  # Buffer for C++ callback cleanup
                except Exception as e:
                    console.print(f"[dim]Callback cleanup error: {e}[/dim]")

            # Final sleep before logout - reduces C++ crash risk
            time.sleep(2)

            logout()

            # Final buffer before process exit - prevents macOS "Python quit unexpectedly" dialog
            time.sleep(3)
            
            console.print("[green]Session logged out cleanly. Exiting...[/green]")
        except Exception as e:
            console.print(f"[dim]Cleanup error: {e}[/dim]")
            # Still sleep to reduce C++ crash risk
            time.sleep(2)
    
def main():
    """Main entry point with macOS signal handling."""
    
    # macOS signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        console.print(f"[yellow]📴 Received signal {signum}. Shutting down gracefully...[/yellow]")
        _shutdown_event.set()
        # Give the main loop time to detect the shutdown
        time.sleep(1)
        sys.exit(0)
    
    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip broker login entirely")
    args = parser.parse_args()
    
    try:
        run_system(dry_run=args.dry_run)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted by user[/yellow]")
        _shutdown_event.set()
        time.sleep(1)
        sys.exit(0)
    except Exception as e:
        console.print(f"[bold red]Unhandled exception in main: {e}[/bold red]")
        _shutdown_event.set()
        time.sleep(1)
        sys.exit(1)

if __name__ == "__main__":
    main()
