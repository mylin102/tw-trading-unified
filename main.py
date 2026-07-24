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
from dataclasses import dataclass, field
from typing import Dict, Any

sys.path.insert(0, os.path.dirname(__file__))

# ── [BOOT_FINGERPRINT] Unambiguous identity marker ──
import core.futures_strategy_router as _boot_fsr
print(
    f"[BOOT_FINGERPRINT] pid={os.getpid()} "
    f"python={sys.executable} "
    f"cwd={os.getcwd()} "
    f"router_file={_boot_fsr.__file__} "
    f"router_mtime={os.path.getmtime(_boot_fsr.__file__):.0f} "
    f"has_evaluate_theta={hasattr(_boot_fsr, '_evaluate_theta_environment')} "
    f"has_no_data={hasattr(_boot_fsr, 'route_futures_signal')}",
    flush=True,
)

from rich.console import Console
from core.date_utils import is_taifex_futures_market_open
from core.shioaji_session import get_api, logout
from strategies.futures.monitor import _thread_local

console = Console()

# ── Lifecycle provenance (module-level for signal handler access) ──
_lifecycle_recorder: Any = None

# [P0 Fix] Connection state tracking via Shioaji event callback
_connection_dropped = False

BASE = os.path.dirname(__file__)
RESTART_FLAG = Path(BASE) / ".restart"
HEALTH_INTERVAL = 30  # seconds between health checks
RESTART_RETRY_LIMIT = 5
RESTART_WINDOW_SECS = 300  # 5 minutes

# macOS graceful shutdown flag
_shutdown_event = threading.Event()

# Feed health tracking (TX/MXF/OPTIONS)
TX_PREFIXES = ("TXF", "TX", "TXO")
MXF_PREFIXES = ("MXF", "TMF")
FEED_STALE_SECS = 120
FEED_WARN_SECS = 45

@dataclass
class FeedHealth:
    lock: threading.Lock = field(default_factory=threading.Lock)
    last_tick_ts: Dict[str, float] = field(default_factory=lambda: {
        "TX": 0.0,
        "MXF": 0.0,
        "OPTIONS": 0.0,
    })
    last_tick_code: Dict[str, str] = field(default_factory=dict)

    def mark_tick(self, bucket: str, code: str):
        now = time.time()
        with self.lock:
            self.last_tick_ts[bucket] = now
            self.last_tick_code[bucket] = code

    def age(self, bucket: str) -> float:
        with self.lock:
            ts = self.last_tick_ts.get(bucket, 0.0)
        if ts <= 0:
            return float("inf")
        return time.time() - ts

    def snapshot(self):
        with self.lock:
            return {
                "ages": {k: (float("inf") if v <= 0 else time.time() - v) for k, v in self.last_tick_ts.items()},
                "codes": dict(self.last_tick_code),
            }


def tick_dispatcher(futures_mons, options_mon, feed_health=None, tx_bar_builder=None):
    """Dispatch futures and options ticks, update feed_health, and build TX bars when provided."""
    if futures_mons is None:
        futures_mons = []
    elif not isinstance(futures_mons, (list, tuple)):
        futures_mons = [futures_mons]

    _seen_codes = set()
    _lock = threading.Lock()

    def classify(code: Any) -> str:
        if not code:
            return "OPTIONS"
        code_str = str(code).upper()
        # [FeedHealth] Synthetic/virtual ticks (TMF_VIRTUAL) must NEVER update real feed health.
        # 2026-05-22 Hermes Agent: VIRTUAL detection prevents feed health pollution
        if "VIRTUAL" in code_str:
            return "VIRTUAL"
        if code_str.startswith("TXF") or code_str.startswith("TX"):
            # Check for TXO (Options)
            if code_str.startswith("TXO"):
                return "OPTIONS"
            return "TX"
        if code_str.startswith("MXF") or code_str.startswith("MX") or code_str.startswith("TMF") or code_str.startswith("TM"):
            return "MXF"
        return "OPTIONS"

    def on_tick(*args):
        # rshioaji 1.5.10+ uses 1-arg callback (data)
        # legacy shioaji uses 2-arg callback (exchange, tick)
        if len(args) == 1:
            tick = args[0]
            exchange = None
        else:
            exchange, tick = args

        # Safety checks
        if _shutdown_event.is_set():
            return
        if tick is None or not hasattr(tick, 'code'):
            return

        code = getattr(tick, 'code', '') or ''
        try:
            with _lock:
                if code not in _seen_codes:
                    _seen_codes.add(code)
                    try:
                        close = getattr(tick, 'close', None)
                        console.print(f"[cyan]📥 New tick code: {code} close={close}[/cyan]")
                    except Exception:
                        console.print(f"[cyan]📥 New tick code: {code}[/cyan]")
        except Exception as e:
            console.print(f"[red][tick tracking err] {e}[/red]")

        # Update feed health (skip synthetic/virtual ticks — they must never pollute real feed health)
        # 2026-05-22 Hermes Agent: skip mark_tick for VIRTUAL bucket; still forward tick to monitors
        try:
            bucket = classify(code)
            if bucket == "VIRTUAL":
                # [VIRTUAL_TICK_FEED_HEALTH_SKIP] Synthetic ticks still forwarded to monitors
                # but must not update feed health buckets (MXF/TX/OPTIONS).
                pass
            elif feed_health is not None:
                # Always mark tick, even if classify logic is slightly off
                feed_health.mark_tick(bucket, str(code))
        except Exception as e:
            if feed_health is not None:
                feed_health.mark_tick("OPTIONS", str(code))
        # TX bar building (optional)
        if bucket == "TX" and tx_bar_builder is not None:
            try:
                tx_bar_builder.on_tick(tick)
            except Exception as e:
                console.print(f"[red][tx tick err] {e}[/red]")

        # Dispatch to monitors
        for f_mon in futures_mons:
            _thread_local.state_path = getattr(f_mon, "_state_path", None)
            try:
                f_mon.on_tick(exchange, tick)
            except Exception as e:
                console.print(f"[red][futures tick err] {e}[/red]")
            finally:
                _thread_local.state_path = None

        try:
            options_mon.on_tick(exchange, tick)
        except Exception as e:
            console.print(f"[red][options tick err] {e}[/red]")

    return on_tick


def order_dispatcher(futures_mons, options_mon):
    """Dispatch order events to both futures and options monitors."""
    if futures_mons is None:
        futures_mons = []
    elif not isinstance(futures_mons, (list, tuple)):
        futures_mons = [futures_mons]

    def on_order_event(order_state, data):
        # Dispatch to futures monitors
        for f_mon in futures_mons:
            _thread_local.state_path = getattr(f_mon, "_state_path", None)
            try:
                if hasattr(f_mon, "on_order_event"):
                    f_mon.on_order_event(order_state, data)
            except Exception as e:
                console.print(f"[red][futures order err] {e}[/red]")
            finally:
                _thread_local.state_path = None

        # Dispatch to options monitor
        try:
            if hasattr(options_mon, "on_order_event"):
                options_mon.on_order_event(order_state, data)
        except Exception as e:
            console.print(f"[red][options order err] {e}[/red]")

    return on_order_event


def bidask_dispatcher(futures_mons, options_mon, skew_engine=None):
    """Route BidAsk updates to monitors for IV calculation and data freshness.

    Args:
        futures_mons: list of FuturesMonitor instances.
        options_mon: OptionsMonitor instance.
        skew_engine: Optional OptionSurfaceEngine — receives OptionQuoteEvent
                     on every option bidask callback for skew calculation.
    """
    if futures_mons is None:
        futures_mons = []
    elif not isinstance(futures_mons, (list, tuple)):
        futures_mons = [futures_mons]
    _seen = set()
    _lock = threading.Lock()
    _last_mtx_update_log_at = {"ts": 0.0}
    _mtx_update_log_interval_secs = 5.0
    
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
                bid = float(bidask.bid_price[0] if hasattr(bidask.bid_price, '__getitem__') else bidask.bid_price)
                ask = float(bidask.ask_price[0] if hasattr(bidask.ask_price, '__getitem__') else bidask.ask_price)
            except (ValueError, TypeError, IndexError):
                return
            
            if bid <= 0 or ask <= 0:
                return
            
            mid = (bid + ask) / 2
            
            # Match by code
            matched = False
            for key in ["C", "P", "MTX"]:
                con = mon.active_contracts.get(key)
                # [GSD Settlement Fix] Strict code matching to avoid cross-month price contamination
                if con and code == getattr(con, "code", None):
                    mon.market_data[key]["bid"] = float(bid)
                    mon.market_data[key]["ask"] = float(ask)
                    mon.market_data[key]["close"] = mid
                    
                    # 💡 GSD: Update freshness timestamp to prevent watchdog from restarting
                    mon.last_tick_at = time.time()
                    for f_mon in futures_mons:
                        if hasattr(f_mon, 'last_tick_at'):
                            f_mon.last_tick_at = time.time()
                    
                    # 💡 GSD: Also update FuturesMonitor's internal market price cache if it exists
                    if key == "MTX":
                        for f_mon in futures_mons:
                            if hasattr(f_mon, 'market_data') and "MTX" in f_mon.market_data:
                                f_mon.market_data["MTX"]["close"] = mid
                    
                    matched = True

                # [Skew Integration] Feed option quote to skew engine
                # 💡 GSD: Only dispatch if the code matches the contract and price is realistic for an option (< 10,000)
                # to avoid price contamination (e.g. MTX price in options)
                # 2026-06-04 Gemini CLI: Added strict code and price validation
                if skew_engine is not None and key in ("C", "P") and code == getattr(con, "code", None) and float(mid) < 10000:
                    try:
                        from core.derivatives import OptionQuoteEvent
                        import datetime as _dt
                        opt_type = "CALL" if key == "C" else "PUT"
                        strike = float(getattr(con, "strike_price", 0))
                        ts = _dt.datetime.now()
                        event = OptionQuoteEvent(
                            timestamp=ts,
                            symbol=code,
                            option_type=opt_type,
                            strike=strike,
                            bid=float(bid),
                            ask=float(ask),
                            mid=mid,
                            expiry=str(getattr(con, "delivery_date", "")),
                        )
                        skew_engine.on_quote(event)
                    except Exception as e:
                        console.print(f"[dim][skew on_quote err] {e}[/dim]")

                # MTX bid/ask updates can arrive at very high frequency near open.
                # Throttle visibility logs so callback cost stays bounded without
                # changing any market-data mutation or freshness semantics.
                if key == "MTX":
                    now = time.time()
                    if now - _last_mtx_update_log_at["ts"] >= _mtx_update_log_interval_secs:
                        _last_mtx_update_log_at["ts"] = now
                        console.print(f"[green]✅ MTX updated ({code}): {mon.market_data['MTX']['close']:.0f}[/green]")
                    break
            
            if not matched and code not in _seen:
                # [Skew Integration] Check if code is an OTM skew contract
                otm_cons = getattr(futures_mon, '_skew_otm_contracts', {})
                if skew_engine is not None and otm_cons:
                    for otm_key, otm_con in otm_cons.items():
                        if code == getattr(otm_con, "code", None):
                            opt_type = "CALL" if "CALL" in otm_key.upper() or "_C" in otm_key else "PUT"
                            try:
                                # 💡 GSD: Only dispatch if price is realistic for an option (< 10,000)
                                # 2026-06-04 Gemini CLI: Added price validation for OTM contracts
                                if float(mid) >= 10000:
                                    continue

                                from core.derivatives import OptionQuoteEvent
                                import datetime as _dt
                                strike = float(getattr(otm_con, "strike_price", 0))
                                event = OptionQuoteEvent(
                                    timestamp=_dt.datetime.now(),
                                    symbol=code,
                                    option_type=opt_type,
                                    strike=strike,
                                    bid=float(bid),
                                    ask=float(ask),
                                    mid=mid,
                                    expiry=str(getattr(otm_con, "delivery_date", "")),
                                )
                                skew_engine.on_quote(event)
                            except Exception as e:
                                console.print(f"[dim][skew otm on_quote err] {e}[/dim]")
                            matched = True
                            break
                if not matched:
                    console.print(f"[yellow]bidask unmatched: {code}, contracts={list(mon.active_contracts.keys())}[/yellow]")
        except Exception as e:
            console.print(f"[red][bidask dispatch err] {e}[/red]")
    
    return on_bidask


def _safe_contract_strike(contract):
    """Safely extract strike_price from a Shioaji option contract."""
    try:
        return int(float(contract.strike_price))
    except Exception:
        return None


def _option_strike_step(strikes: list[int]) -> int:
    """Infer strike step from sorted unique strikes."""
    if len(strikes) < 2:
        return 200  # default TXO step
    diffs = sorted(set(abs(strikes[i + 1] - strikes[i]) for i in range(len(strikes) - 1)))
    return min(diffs)


def _nearest_strike(strikes: list[int], target: int) -> int | None:
    """Return the strike closest to target, or None if empty."""
    if not strikes:
        return None
    return min(strikes, key=lambda x: abs(x - target))


def _subscribe_otm_skew_contracts(api, om, fm, sk_engine, console=None):
    """Resolve and subscribe OTM option contracts for skew surface.

    Phase 1.5 diagnostic: dumps the full option universe, infers strike step,
    resolves ATM from the nearest available strike to the anchor, and
    subscribes OTM puts/calls aligned to the strike grid.

    The anchor *for OTM subscribe* is the ATM call strike (from active_contracts)
    because we need a subscribe-time value.  The actual ATM anchor *for skew
    computation* is determined at compute-time from live futures_price and is
    independent of this function.
    """
    if api is None:
        return {}
    otm_contracts = {}
    if console is None:
        console = print
    _log = console.print if hasattr(console, 'print') else console

    try:
        # --- 1. get the full option universe for the active month ---
        all_contracts = getattr(om.monitor, "_all_month_contracts", None)
        if not all_contracts:
            target_m = om.monitor.get_futures_contract_month(fm)
            _, all_contracts = om.monitor.get_options_by_month("TXO", target_m)

        if not all_contracts:
            _log("[yellow][OptionSkew] no contracts found; skip OTM subscribe[/yellow]")
            fm._skew_otm_contracts = {}
            return {}

        # Build sorted strike lists for call / put
        call_strikes = sorted(
            _safe_contract_strike(c) for c in all_contracts
            if _safe_contract_strike(c) is not None and "Call" in str(c.option_right)
        )
        put_strikes = sorted(
            _safe_contract_strike(c) for c in all_contracts
            if _safe_contract_strike(c) is not None and "Put" in str(c.option_right)
        )

        inferred_step = _option_strike_step(call_strikes + put_strikes)

        # --- 2. resolve subscribe-time anchor ---
        # Use the ATM call strike from active_contracts as the subscribe anchor.
        # This is a best-effort anchor for OTM subscribe only.  The compute-time
        # anchor is resolved from live futures_price inside SurfaceEngine.
        atm_call = om.monitor.active_contracts.get("C")
        atm_put = om.monitor.active_contracts.get("P")
        call_atm_strike = _safe_contract_strike(atm_call)
        put_atm_strike = _safe_contract_strike(atm_put)

        if call_atm_strike is not None and put_atm_strike is not None:
            if abs(call_atm_strike - put_atm_strike) <= 100:
                skew_anchor = int(round((call_atm_strike + put_atm_strike) / 2 / inferred_step) * inferred_step)
            else:
                _log(
                    "[yellow][OptionSkew] ATM C/P mismatch: "
                    f"call={call_atm_strike}, put={put_atm_strike}; "
                    "use call strike as temporary anchor[/yellow]"
                )
                skew_anchor = call_atm_strike
        elif call_atm_strike is not None:
            skew_anchor = call_atm_strike
        else:
            _log("[yellow][OptionSkew] Cannot resolve ATM strike; skip OTM subscribe[/yellow]")
            fm._skew_otm_contracts = {}
            return {}

        # --- 3. diagnostic dump ---
        _log(
            "[OptionSkew][Universe] month=E6 "
            f"call_strikes={call_strikes[:6]}... (total {len(call_strikes)}) "
            f"put_strikes={put_strikes[:6]}... (total {len(put_strikes)})"
        )
        _log(
            f"[OptionSkew][Step] inferred_step={inferred_step}"
        )
        _log(
            f"[OptionSkew][ATM] call_atm={call_atm_strike}, put_atm={put_atm_strike}, "
            f"anchor={skew_anchor}"
        )

        # --- 4. resolve OTM targets aligned to strike grid ---
        otm_call_target = skew_anchor + sk_engine.otm_points
        otm_put_target = skew_anchor - sk_engine.otm_points

        otm_call_strike = _nearest_strike(call_strikes, otm_call_target)
        otm_put_strike = _nearest_strike(put_strikes, otm_put_target)

        _log(
            f"[OptionSkew][Targets] put_target={otm_put_target}, "
            f"call_target={otm_call_target}"
        )
        _log(
            f"[OptionSkew][Resolved] otm_put={otm_put_strike}, "
            f"otm_call={otm_call_strike}"
        )

        # --- 5. subscribe the resolved OTM contracts ---
        import shioaji as sj
        # Find OTM put
        if otm_put_strike is not None:
            for c in all_contracts:
                strike = _safe_contract_strike(c)
                if strike == otm_put_strike and "Put" in str(c.option_right):
                    otm_contracts["OTM_P"] = c
                    from core.broker.shioaji_compat import safe_subscribe
                    safe_subscribe(api, c, quote_type="tick")
                    safe_subscribe(api, c, quote_type="bidask")
                    _log(
                        "[green][OptionSkew] subscribed otm_put: "
                        f"{c.code} (strike={otm_put_strike})[/green]"
                    )
                    break
        # Find OTM call
        if otm_call_strike is not None:
            for c in all_contracts:
                strike = _safe_contract_strike(c)
                if strike == otm_call_strike and "Call" in str(c.option_right):
                    otm_contracts["OTM_C"] = c
                    from core.broker.shioaji_compat import safe_subscribe
                    safe_subscribe(api, c, quote_type="tick")
                    safe_subscribe(api, c, quote_type="bidask")
                    _log(
                        "[green][OptionSkew] subscribed otm_call: "
                        f"{c.code} (strike={otm_call_strike})[/green]"
                    )
                    break

        if not otm_contracts:
            _log(
                "[yellow][OptionSkew] No OTM contracts resolved for "
                f"anchor={skew_anchor} ±{sk_engine.otm_points} "
                f"(otm_call={otm_call_strike}, otm_put={otm_put_strike})[/yellow]"
            )

    except Exception as e:
        _log(f"[yellow]⚠️ _subscribe_otm_skew_contracts error: {e}[/yellow]")
        import traceback
        _log(f"[yellow]{traceback.format_exc()}[/yellow]")

    fm._skew_otm_contracts = otm_contracts
    return otm_contracts


def api_is_healthy(api):
    """Quick check if Shioaji session is still usable, with a small retry."""
    if api is None:
        return False
    for attempt in range(2):
        try:
            api.list_positions(api.futopt_account)
            # Success → mark account healthy
            try:
                from core.channel_safety import get_safety_state
                get_safety_state().set_account_healthy()
            except Exception:
                pass
            return True
        except Exception as exc:
            if attempt == 0:
                time.sleep(1)
            else:
                msg = f"{type(exc).__name__}: {exc}"
                console.print(f"[dim]api_is_healthy: list_positions failed (2 attempts): {msg}[/dim]")
                # Mark account degraded if it's a connection error
                exc_name = type(exc).__name__
                if "ConnectionError" in exc_name or "ShioajiConnectionError" in exc_name or "NotReady" in str(exc):
                    try:
                        from core.channel_safety import get_safety_state, AccountDegradedReason
                        get_safety_state().set_account_degraded(AccountDegradedReason.SHIOAJI_CONNECTION_ERROR, msg)
                    except Exception:
                        pass
    return False


def _try_shioaji_reconnect(api, fm, om, dry_run: bool) -> bool:
    """Attempt in-process Shioaji re-login + re-subscribe after data stagnation.

    Returns True if reconnect succeeded and system can continue.
    Returns False if reconnect failed (network down or session expired) —
    caller should exit for PM2 restart.

    Strategy:
    1. If Shioaji event callback already detected drop (code 12), wait for it
    2. Otherwise, attempt full re-login
    3. On network failure, wait with backoff before giving up
    """
    if dry_run:
        return True  # nothing to reconnect in dry_run mode

    global _connection_dropped

    # ── Phase 1: Shioaji auto-reconnect already in progress ──
    if _connection_dropped:
        console.print("[yellow]⏳ Shioaji auto-reconnect (code 12) in progress — waiting 30s...[/yellow]")
        time.sleep(30)
        if not _connection_dropped:
            console.print("[green]✅ Shioaji auto-reconnect completed[/green]")
            return True
        console.print("[yellow]⚠️ Shioaji auto-reconnect still pending — attempting manual re-login[/yellow]")

    # ── Phase 2: Manual re-login ──
    from core.broker.shioaji_compat import safe_login, set_tick_callback, set_bidask_callback
    import os

    api_key = os.getenv("SHIOAJI_API_KEY")
    secret_key = os.getenv("SHIOAJI_SECRET_KEY")

    if not api_key or not secret_key:
        console.print("[red]❌ Missing API credentials — cannot reconnect[/red]")
        return False

    for attempt in range(3):
        try:
            console.print(f"[cyan]🔄 Re-login attempt {attempt+1}/3...[/cyan]")
            safe_login(api, api_key, secret_key, contracts_timeout=10000)
            console.print("[green]✅ Shioaji re-login successful[/green]")

            # Re-subscribe all contracts
            if fm and fm.contract:
                api.quote.subscribe(fm.contract, quote_type='tick')
                console.print(f"[dim]📡 Re-subscribed: {fm.contract.code}[/dim]")
            if fm and fm.far_contract:
                api.quote.subscribe(fm.far_contract, quote_type='tick')
                console.print(f"[dim]📡 Re-subscribed far: {fm.far_contract.code}[/dim]")
            if om:
                for key in ["MTX", "C", "P"]:
                    con = om.monitor.active_contracts.get(key)
                    if con:
                        api.quote.subscribe(con, quote_type='tick')
                        if key != "MTX":
                            import shioaji as sj
                            api.quote.subscribe(con, quote_type=sj.constant.QuoteType.BidAsk)

            # Re-wire tick callbacks (may have been lost)
            from core.broker.shioaji_compat import set_tick_callback, set_bidask_callback
            # tick_dispatcher is defined in the outer scope — we pass None for now,
            # the existing callbacks should still be wired
            _connection_dropped = False
            return True

        except Exception as e:
            console.print(f"[yellow]⚠️ Re-login attempt {attempt+1} failed: {e}[/yellow]")
            if attempt < 2:
                wait = (attempt + 1) * 15  # 15s, 30s, 45s backoff
                console.print(f"[dim]Waiting {wait}s before retry...[/dim]")
                time.sleep(wait)

    console.print("[red]❌ All re-login attempts failed — network may be down[/red]")
    return False


def _setup_event_callback(api, fm, om):
    """[P0 Fix] Monitor Shioaji connection state via event callback.

    Event codes:
      12 = RECONNECTING_NOTICE (session dropped, reconnecting)
      13 = RECONNECTED_NOTICE (reconnected successfully)
      16 = SUBSCRIPTION_OK (subscription confirmed)
      20 = REPUBLISH_UNACKED (unknown publisher flow — needs resubscribe)
    """
    if api is None:
        return
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




def feeds_are_fresh(feed_health, require_tx=True, require_futures=True):
    snap = feed_health.snapshot()
    ages = snap.get('ages', {})
    problems = []
    fut_age = ages.get('MXF', float('inf'))
    tx_age = ages.get('TX', float('inf'))
    if require_futures and fut_age > FEED_STALE_SECS:
        problems.append(f"FUTURES (MXF/TMF) stale: {fut_age:.0f}s")
    # 💡 GSD: TX stale is non-fatal for process survival
    if require_tx and tx_age > FEED_STALE_SECS:
        if tx_age == float('inf'):
            console.print("[bold yellow]⚠️ TX data NEVER received - continuing in degraded mode[/bold yellow]")
        else:
            console.print(f"[yellow]⚠️ TX stale: {tx_age:.0f}s[/yellow]")
    return (len(problems) == 0), problems, snap


def run_system(dry_run=False, config_name="futures"):
    """運行交易系統，遇到斷線或重啟請求時結束進程，由外部腳本重新拉起"""
    # 啟動時立即清除重啟旗標，避免循環重啟
    if RESTART_FLAG.exists():
        RESTART_FLAG.unlink()
        console.print("[dim]Old restart flag cleared.[/dim]")

    # ── Lifecycle provenance recorder ──
    from core.lifecycle_provenance import LifecycleRecorder
    global _lifecycle_recorder
    _lifecycle_recorder = LifecycleRecorder()
    _rec = _lifecycle_recorder
    _rec.record_event("PROCESS_START", pm2_process_id=os.getpid())

    # ── Reset safety state for new process lifecycle ──
    try:
        from core.channel_safety import reset_safety_state, get_safety_state
        reset_safety_state()
    except Exception:
        pass

    api = None
    # Login retry with exponential backoff — prevents crash loop when Shioaji server
    # temporarily blocks rapid reconnects. Retries up to 5x internally so PM2 never
    # sees the crash and restarts stay at 0.
    _login_attempt = 0
    _login_max_attempts = 5
    while True:
        try:
            if not dry_run:
                api = get_api()
                console.print("[green]✅ Single Shioaji session established[/green]")
                break
            else:
                break  # dry-run: no login needed
        except Exception as _login_err:
            _login_attempt += 1
            if _login_attempt >= _login_max_attempts:
                console.print(f"[bold red]🚨 All {_login_max_attempts} login attempts exhausted. Exiting for external supervisor.[/bold red]")
                import sys
                sys.exit(1)
            _delay = min(15 * (2 ** (_login_attempt - 1)), 120)  # 15s, 30s, 60s, 120s capped
            console.print(
                f"[bold red]Critical crash: {_login_err}[/bold red]",
                f"\n[yellow]⏳ Login attempt {_login_attempt}/{_login_max_attempts} failed — "
                f"retrying in {_delay}s...[/yellow]"
            )
            time.sleep(_delay)

    try:
        if not dry_run:
            api = get_api()
            console.print("[green]✅ Single Shioaji session established[/green]")

            # Mark safety state as reconciled (post-restart, fresh login)
            try:
                from core.channel_safety import get_safety_state
                get_safety_state().set_reconciled()
            except Exception:
                pass

            # ── Shioaji quote event callback (provenance only) ──
            try:
                import shioaji as sj  # noqa: used for quote_event typing
                def _on_quote_event(resp_code: int, event_code: int, info: str, event: str) -> None:
                    _rec.record_event(
                        "QUOTE_EVENT",
                        quote_event_code=event_code,
                        quote_event_name=event,
                        quote_info=info[:200] if info else None,
                    )
                api.quote_event = _on_quote_event
                _rec.record_event("QUOTE_CALLBACK_REGISTERED")
            except Exception as _qc_err:
                console.print(f"[dim]Quote event callback registration skipped: {_qc_err}[/dim]")

            # [rshioaji 1.5.10+] Ensure contracts are loaded in cache before monitors start
            from core.broker.shioaji_compat import fetch_all_contracts
            console.print("[cyan]📡 Synchronizing all broker contracts (this may take 1-5 minutes)...[/cyan]")
            print(f"[CONTRACT_SYNC_START] timeout=300s", flush=True)
            if fetch_all_contracts(api, timeout=300):
                console.print("[green]✅ Contracts synchronized successfully.[/green]")
                print(f"[CONTRACT_SYNC_PROGRESS] success", flush=True)
            else:
                console.print("[yellow]⚠️ Contract synchronization timed out, continuing with best effort.[/yellow]")
                print(f"[CONTRACT_SYNC_TIMEOUT] elapsed>=300s, continuing with partial contracts", flush=True)
        else:
            console.print("[yellow]🔧 Dry-run — no broker login[/yellow]")

        from strategies.futures.monitor import FuturesMonitor

        # [GSD] Session-aware config: night uses futures_night.yaml (wider stops, longer VWAP confirm)
        # 2026-07-21 Gemini CLI: support comma-separated list of config names (e.g. "futures,futures_mtx")
        from core.date_utils import is_night_session
        from datetime import datetime as _dt
        _is_night = is_night_session(_dt.now())
        
        config_names = [c.strip() for c in config_name.split(",") if c.strip()]
        futures_mons = []
        for cfg_item in config_names:
            _base_cfg = cfg_item.replace(".yaml", "")
            if _is_night:
                _config_file = f"{_base_cfg}_night.yaml"
                if not os.path.exists(os.path.join(BASE, "config", _config_file)):
                    _config_file = f"{_base_cfg}.yaml"
            else:
                _config_file = f"{_base_cfg}.yaml"
            console.print(f"[dim]📋 Futures config for {cfg_item}: {_config_file} (session={'night' if _is_night else 'day'})[/dim]")

            fm_inst = FuturesMonitor(
                api=api,
                config_path=os.path.join(BASE, "config", _config_file),
                dry_run=dry_run,
            )
            fm_inst.setup()
            futures_mons.append(fm_inst)

        if not futures_mons:
            raise RuntimeError("No valid futures configurations loaded")

        from strategies.options.monitor import OptionsMonitor
        om = OptionsMonitor(api=api, dry_run=dry_run)
        
        # Share options monitor reference across all futures monitors
        # fm.options_monitor = om.monitor
        for f_mon in futures_mons:
            f_mon.options_monitor = om.monitor

        # ── MTX Market Data Runtime ──
        # 被動收集 MTX tick 資料，不下單、不跑策略。
        # TMF callback 保持原位，GlobalCallbackAdapter 在 callback registration 時安裝。
        _mtx_runtime: Any = None
        if not dry_run:
            from core.market_data_runtime import build_mtx_runtime
            from core.market_data_registry import MarketDataRegistry
            from strategies.futures.market_data_collector import MarketDataCollector

            # Build MTX contract resolver using Shioaji API
            from strategies.futures.contract_resolver import MarketDataContractResolver
            _mtx_resolver_obj = MarketDataContractResolver(
                api=api,
                trading_date_provider=lambda: _dt.now().date(),
            )
            # Wrap ResolvedContracts → tuple for collector compatibility
            def _mtx_resolver_fn(ticker: str) -> tuple[Any | None, Any | None]:
                result = _mtx_resolver_obj.resolve_near_far(ticker)
                if result is None:
                    return None, None
                return result.near_raw, result.far_raw

            try:
                _mtx_registry = MarketDataRegistry()
                _mtx_runtime = build_mtx_runtime(
                    registry=_mtx_registry,
                    fallback_tick=lambda *a: None,  # 暫時 no-op，callback registration 時取代
                    resolver=_mtx_resolver_fn,
                )
                console.print("[dim]📊 MTX market data runtime initialized (passive)[/dim]")
            except Exception as exc:
                console.print(f"[yellow]⚠️ MTX runtime init failed: {exc} — continuing without MTX[/yellow]")
                _mtx_runtime = None

        # Create threading.Thread objects for all monitors
        futures_threads = []
        for f_mon in futures_mons:
            ft = threading.Thread(target=f_mon.run, name=f"futures_{f_mon.ticker.lower()}", daemon=True)
            futures_threads.append((f_mon, ft))

        # GSD Rationale: Stock module moved to scripts/stock_runner.py for fault isolation.
        # main.py now only handles Futures + Options which share the FOP callback session.

        # 先初始化 contracts，再訂閱 (使用第一個期貨監控器作為基準同步月份)
        om.monitor.find_best_contracts(futures_mons[0])
        om.monitor.pre_fill_bars()

        # [Skew Integration] Initialize option surface engine
        try:
            from core.derivatives import OptionSurfaceEngine
            sk_engine = OptionSurfaceEngine(otm_points=300)
        except Exception:
            sk_engine = None

        if api is not None:
            import shioaji as sj
            # Initialize feed health tracker
            feed_health = FeedHealth()

            # Initialize TX bar builder for cross-regime (optional)
            try:
                from strategies.cross_regime import TxBarBuilder
                tx_bar_builder = TxBarBuilder()
            except Exception:
                tx_bar_builder = None

            # Register tick/bidask callbacks with feed health and tx builder
            from core.broker.shioaji_compat import set_tick_callback, set_bidask_callback, safe_subscribe
            _tmf_tick_fn = tick_dispatcher(futures_mons, om, feed_health, tx_bar_builder)
            _tmf_bidask_fn = bidask_dispatcher(futures_mons, om, sk_engine)

            # Install GlobalCallbackAdapter so MTX ticks are routed to passive collector AND tick_dispatcher
            if _mtx_runtime is not None:
                from core.global_callback_adapter import GlobalCallbackAdapter
                # 💡 Gemini CLI: Set always_call_fallback=True so tick_dispatcher (TMF + MTX monitors) receives ticks alongside passive collector
                _mtx_runtime.adapter = GlobalCallbackAdapter(
                    registry=_mtx_runtime.registry,
                    fallback_tick_handler=_tmf_tick_fn,
                    fallback_bidask_handler=_tmf_bidask_fn,
                    always_call_fallback=True,
                )
                set_tick_callback(api, _mtx_runtime.adapter.on_tick)
                set_bidask_callback(api, _mtx_runtime.adapter.on_bidask)
                console.print("[green]✅ GlobalCallbackAdapter installed (TMF + MTX routing with fallback dispatch)[/green]")

                # Start MTX runtime (resolves contracts, binds routes, starts writer)
                if _mtx_runtime.start():
                    console.print("[green]✅ MTX market data runtime started[/green]")

                    # ── Start health evidence sampler ──
                    try:
                        from core.health_evidence_exporter import HealthEvidenceSampler
                        _run_id = _dt.now().strftime("%Y%m%d-%H%M%S")
                        _mtx_sampler = HealthEvidenceSampler(
                            health_fn=_mtx_runtime.health,
                            product_code="MXF",
                            run_id=_run_id,
                            interval_sec=30.0,
                        )
                        _mtx_sampler.start()
                        console.print(f"[dim]📊 Health sampler started (run={_run_id})[/dim]")
                    except Exception as exc:
                        console.print(f"[yellow]⚠️ Health sampler start failed: {exc}[/yellow]")
                        _mtx_sampler = None
                else:
                    console.print("[yellow]⚠️ MTX runtime start failed — contracts may not resolve[/yellow]")
            else:
                set_tick_callback(api, _tmf_tick_fn)
                set_bidask_callback(api, _tmf_bidask_fn)

            # [P0 Fix] Centralized order dispatcher to avoid "Already borrowed" error
            api.set_order_callback(order_dispatcher(futures_mons, om))
            console.print("[green]✅ Order event callback registered[/green]")

            # [Skew Integration] Wire skew engine into FuturesMonitor for strategy context
            for f_mon in futures_mons:
                f_mon._skew_engine = sk_engine

            # Subscribe ticks for all monitors
            for f_mon in futures_mons:
                if f_mon.contract is not None:
                    try:
                        safe_subscribe(api, f_mon.contract, quote_type='tick')
                        console.print(f"[green]📡 Subscribed {f_mon.ticker} tick: {f_mon.contract.code}[/green]")
                    except Exception as e:
                        console.print(f"[yellow]⚠️ {f_mon.ticker} subscribe failed: {e}[/yellow]")

                if f_mon.far_contract is not None:
                    try:
                        safe_subscribe(api, f_mon.far_contract, quote_type='tick')
                        console.print(f"[green]📡 Subscribed far-month {f_mon.ticker} tick: {f_mon.far_contract.code}[/green]")
                    except Exception as e:
                        console.print(f"[yellow]⚠️ Far-month {f_mon.ticker} subscribe failed: {e}[/yellow]")

            # ── Subscribe MTX ticks (passive collection) ──
            if _mtx_runtime is not None and _mtx_runtime.collector.is_resolved:
                # Use the raw Shioaji contract objects from the resolver
                _mtx_result = _mtx_resolver_obj.resolve_near_far("MXF")
                if _mtx_result is not None:
                    try:
                        safe_subscribe(api, _mtx_result.near_raw, quote_type='tick')
                        console.print(f"[green]📡 Subscribed MTX tick: {_mtx_result.near_code}[/green]")
                    except Exception as e:
                        console.print(f"[yellow]⚠️ MTX near subscribe failed: {e}[/yellow]")
                    try:
                        safe_subscribe(api, _mtx_result.far_raw, quote_type='tick')
                        console.print(f"[green]📡 Subscribed MTX far tick: {_mtx_result.far_code}[/green]")
                    except Exception as e:
                        console.print(f"[yellow]⚠️ MTX far subscribe failed: {e}[/yellow]")

            # Subscribe options (ATM + OTM for skew)
            for key in ["MTX", "C", "P"]:
                con = om.monitor.active_contracts.get(key)
                if con:
                    safe_subscribe(api, con, quote_type='tick')
                    # 2026-06-26 Gemini CLI: MTX only needs tick data for close reference; do not subscribe to bidask to reduce bandwidth/rate limits
                    if key != "MTX":
                        safe_subscribe(api, con, quote_type='bidask')
                        console.print(f"[green]📡 Subscribed {key}: {con.code} (tick+bidask)[/green]")
                    else:
                        console.print(f"[green]📡 Subscribed {key}: {con.code} (tick only)[/green]")

        if sk_engine is not None:
            # Wire OTM skew contracts using the first monitor
            otm_contracts = _subscribe_otm_skew_contracts(
                api, om, futures_mons[0], sk_engine,
                console=console,
            )
        else:
            otm_contracts = {}

        # [P0 Fix] Setup connection event monitoring
        _setup_event_callback(api, futures_mons, om)
        console.print("[green]✅ Connection event callback registered[/green]")
        # Re-enable when futures MTS is stable and options logic is revised.
        _options_enabled = False
        if _options_enabled:
            ot = threading.Thread(target=om.run, name="options", daemon=True)
            ot.start()
        
        for fm, ft in futures_threads:
            ft.start()
        console.print(f"[bold green]🚀 Unified Monitors Running ({', '.join(fm.ticker for fm in futures_mons)})[/bold green]")
        for fm, ft in futures_threads:
            print(f"[RUNTIME_LOOP_STARTED] futures_thread={ft.ident} ticker={fm.ticker}", flush=True)

        startup_grace_until = time.time() + 60
        health_check_at = time.time() + HEALTH_INTERVAL

        # [gstack Sentinel] 數據新鮮度追蹤 — 二次確認防誤判
        last_data_at = time.time()
        stagnation_warned = False  # 第一次只警告，第二次才重啟
        max_restarts = 5  # Prevent infinite restart loop
        restart_count = 0

        while restart_count < max_restarts:
            # [Auto-Restart] Check if threads died unexpectedly
            dead_futures = [fm.ticker for fm, ft in futures_threads if not ft.is_alive()]
            if dead_futures:
                dead = [f"futures_{t.lower()}" for t in dead_futures]
                if _options_enabled and 'ot' in locals() and not ot.is_alive():
                    dead.append("options")
                console.print(f"[bold red]💀 Thread died: {', '.join(dead)}. Restarting all (attempt {restart_count+1}/{max_restarts})...[/bold red]")
                restart_count += 1

                # Re-initialize monitors and threads
                try:
                    from strategies.futures.monitor import FuturesMonitor
                    # [GSD] Session-aware config on restart too
                    # 2026-07-21 Gemini CLI: support custom config names in restart loop
                    from core.date_utils import is_night_session
                    from datetime import datetime as _dt2
                    _is_night = is_night_session(_dt2.now())
                    
                    futures_mons = []
                    for cfg_item in config_names:
                        _base_cfg = cfg_item.replace(".yaml", "")
                        if _is_night:
                            _config_file = f"{_base_cfg}_night.yaml"
                            if not os.path.exists(os.path.join(BASE, "config", _config_file)):
                                _config_file = f"{_base_cfg}.yaml"
                        else:
                            _config_file = f"{_base_cfg}.yaml"
                        
                        fm_inst = FuturesMonitor(
                            api=api,
                            config_path=os.path.join(BASE, "config", _config_file),
                            dry_run=dry_run,
                        )
                        fm_inst.setup()
                        futures_mons.append(fm_inst)

                    from strategies.options.monitor import OptionsMonitor
                    om = OptionsMonitor(api=api, dry_run=dry_run)
                    for f_mon in futures_mons:
                        f_mon.options_monitor = om.monitor

                    # Re-subscribe
                    if api is not None:
                        import shioaji as sj
                        # Reuse existing feed_health if present, else create
                        try:
                            feed_health
                        except NameError:
                            feed_health = FeedHealth()

                        # Ensure tx_bar_builder exists or create
                        try:
                            tx_bar_builder
                        except NameError:
                            try:
                                from strategies.cross_regime import TxBarBuilder
                                tx_bar_builder = TxBarBuilder()
                            except Exception:
                                tx_bar_builder = None

                        from core.broker.shioaji_compat import set_tick_callback, set_bidask_callback
                        set_tick_callback(api, tick_dispatcher(futures_mons, om, feed_health, tx_bar_builder))
                        set_bidask_callback(api, bidask_dispatcher(futures_mons, om, sk_engine))
                        
                        # [Skew Integration] Re-wire skew engine (might have been reset)
                        for f_mon in futures_mons:
                            f_mon._skew_engine = sk_engine
                        if sk_engine is not None:
                            sk_engine.reset()

                        for f_mon in futures_mons:
                            if f_mon.contract is not None:
                                api.quote.subscribe(f_mon.contract, quote_type='tick')

                            if f_mon.far_contract is not None:
                                try:
                                    api.quote.subscribe(f_mon.far_contract, quote_type='tick')
                                    console.print(f"[green]📡 Re-Subscribed far-month {f_mon.ticker} Futures tick: {f_mon.far_contract.code}[/green]")
                                except Exception as e:
                                    console.print(f"[yellow]⚠️ Re-subscribe far-month {f_mon.ticker} Futures failed: {e}[/yellow]")

                        om.monitor.find_best_contracts(futures_mons[0])
                        om.monitor.pre_fill_bars()
                        for key in ["MTX", "C", "P"]:
                            con = om.monitor.active_contracts.get(key)
                            if con:
                                api.quote.subscribe(con, quote_type='tick')
                                if key != "MTX":
                                    api.quote.subscribe(con, quote_type=sj.constant.QuoteType.BidAsk)

                    futures_threads = []
                    for f_mon in futures_mons:
                        ft = threading.Thread(target=f_mon.run, name=f"futures_{f_mon.ticker.lower()}", daemon=True)
                        futures_threads.append((f_mon, ft))
                        ft.start()
                    
                    if _options_enabled:
                        ot = threading.Thread(target=om.run, name="options", daemon=True)
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
            
            # [GSD Hardening] Heartbeat check — FuturesMonitor may be alive but frozen
            for fm, ft in futures_threads:
                _fm_hb = getattr(fm, 'last_heartbeat_ts', 0)
                if _fm_hb > 0 and (now - _fm_hb) > 360:
                    console.print(f"[bold red]💀 {fm.ticker} FuturesMonitor heartbeat stale ({now - _fm_hb:.0f}s). Setting last_tick_at=0 to trigger stale restart...[/bold red]")
                    fm.last_tick_at = 0

            # 檢查任何 FOP tick 是否有進來 (TMF 成交量低，單獨追蹤會誤判)
            fm_last = max(getattr(fm, 'last_tick_at', 0) for fm, ft in futures_threads)
            om_last = getattr(om.monitor, 'last_tick_at', 0)
            latest_tick = max(fm_last, om_last)
            
            if latest_tick > last_data_at:
                last_data_at = latest_tick
                stagnation_warned = False  # tick 恢復，重置警告
            
            # 哨兵邏輯：二次確認 — 3 分鐘警告，5 分鐘就重啟（全天候監控，含夜盤）
            # 💡 GSD: 在 08:45 開盤前與 05:00 盤後寬限處理
            import datetime as _dt_inner
            now_dt = _dt_inner.datetime.now()
            # 判斷是否在開盤前的空窗期 (05:00 - 08:45) 或日盤後至夜盤前的空窗期 (13:45 - 15:00)
            is_pre_market = (now_dt.hour == 8 and now_dt.minute < 45) or (now_dt.hour >= 5 and now_dt.hour < 8)
            is_market_break = (now_dt.hour == 13 and now_dt.minute >= 45) or (now_dt.hour == 14)
            
            stale_secs = now - last_data_at
            stale_limit = 7200 if (is_pre_market or is_market_break) else 300 # 空窗期給予兩小時寬限
            
            if stale_secs > stale_limit:
                console.print(f"[bold red]🚨 DATA STAGNATION CONFIRMED! No data for {stale_secs/60:.1f} mins (fm={now-fm_last:.1f}s, om={now-om_last:.1f}s ago). Attempting in-process reconnect...[/bold red]")

                # 2026-07-07 Hermes Agent: Try in-process reconnect before
                # resorting to process exit + PM2 restart.  If the network
                # (VPN) is down, a full restart is pointless — better to
                # wait in-process and retry re-login with backoff.
                if _try_shioaji_reconnect(api, futures_mons, om if 'om' in dir() else None, dry_run):
                    console.print("[green]✅ In-process reconnect successful — resetting staleness timer[/green]")
                    last_data_at = time.time()
                    stagnation_warned = False
                    # Reset feed health timers so we don't immediately trigger again
                    for bucket in ['TX', 'MXF', 'OPTIONS']:
                        feed_health.last_tick_ts[bucket] = time.time()
                    continue
                else:
                    console.print("[red]💀 Reconnect failed — exiting for PM2 restart[/red]")
                    break
            elif stale_secs > (stale_limit * 0.6) and not stagnation_warned:
                console.print(f"[bold yellow]⚠️ DATA WARNING: No data for {stale_secs/60:.1f} mins. Watching...[/bold yellow]")
                stagnation_warned = True
            
            if RESTART_FLAG.exists():
                RESTART_FLAG.unlink()
                console.print("[bold yellow]🔄 Restart requested. Exiting for external supervisor...[/bold yellow]")
                break

            now = time.time()
            if not dry_run and now > startup_grace_until and now > health_check_at:
                # 1) API session health
                if not api_is_healthy(api):
                    console.print("[red]💀 Shioaji session dead — exiting for external supervisor[/red]")
                    break

                # 2) Feed freshness health
                try:
                    require_tx = any(bool(getattr(fm, 'MONITOR', {}).get('require_tx', True)) for fm, ft in futures_threads)
                except Exception:
                    require_tx = False
                try:
                    require_futures = any(fm.contract is not None for fm, ft in futures_threads)
                except Exception:
                    require_futures = False

                ok, problems, snap = feeds_are_fresh(feed_health, require_tx=require_tx, require_futures=require_futures)
                ages = snap.get('ages', {})
                console.print(
                    "[dim]"
                    f"feed health | TX={ages.get('TX', float('inf')):.0f}s "
                    f"MXF={ages.get('MXF', float('inf')):.0f}s "
                    f"OPT={ages.get('OPTIONS', float('inf')):.0f}s"
                    "[/dim]"
                )

                # Warn before critical
                if snap['ages'].get('MXF', float('inf')) > FEED_WARN_SECS:
                    console.print(f"[yellow]Warning: FUTURES feed quiet for {snap['ages'].get('MXF', 0):.0f}s[/yellow]")
                if require_tx and snap['ages'].get('TX', float('inf')) > FEED_WARN_SECS:
                    console.print(f"[yellow]Warning: TX feed quiet for {snap['ages'].get('TX', 0):.0f}s[/yellow]")

                if not ok:
                    if not is_taifex_futures_market_open():
                        console.print("[dim]Feed stale during scheduled recess — keep process alive[/dim]")
                        health_check_at = now + HEALTH_INTERVAL
                        time.sleep(2)
                        continue
                    console.print(
                        "[bold red]Feed stale — exiting for external supervisor: "
                        + "; ".join(problems)
                        + "[/bold red]"
                    )
                    break

                health_check_at = now + HEALTH_INTERVAL
            time.sleep(2)

    except Exception as exc:
        import traceback
        console.print(f"[bold red]Critical crash: {exc}[/bold red]")
        console.print(traceback.format_exc())
        # 2026-06-15 Hermes Agent: Enforce 15s cooldown on crash to respect Shioaji API limits
        console.print("[yellow]Sleeping 15s to prevent rapid reconnect bans...[/yellow]")
        time.sleep(15)

    finally:
        console.print("[red]⚠️ Exited main loop[/red]")
        _shutdown_event.set()

        # Lifecycle: record shutdown
        _rec = _lifecycle_recorder
        if _rec is not None:
            try:
                _rec.set_shutdown_cause("MAIN_LOOP_RETURNED", "MAIN_LOOP")
                _rec.record_event("MAIN_LOOP_RETURNED")
                _rec.record_event("SHUTDOWN_BEGIN")
            except Exception:
                pass

        # ── Wait for session to become idle before logout ──
        console.print("[dim]Stopping monitors and threads...[/dim]")
        try:
            if 'futures_mons' in locals():
                for f_mon in futures_mons:
                    f_mon.stop()
            elif 'fm' in locals():
                fm.stop()
            if 'om' in locals():
                om.stop()
            if 'sm' in locals():
                sm.stop()

            # Give threads time to finish current operations
            time.sleep(1)

            # Stop MTX runtime before clearing callbacks
            if '_mtx_runtime' in dir() and _mtx_runtime is not None:
                try:
                    _mtx_runtime.stop(timeout=3.0)
                    console.print("[dim]MTX market data runtime stopped[/dim]")
                except Exception as exc:
                    console.print(f"[yellow]⚠️ MTX runtime stop error: {exc}[/yellow]")

            # Stop health sampler
            if '_mtx_sampler' in dir() and _mtx_sampler is not None:
                try:
                    _mtx_sampler.stop(timeout=3.0, final_sample=True)
                    console.print("[dim]Health sampler stopped[/dim]")
                except Exception as exc:
                    console.print(f"[yellow]⚠️ Health sampler stop error: {exc}[/yellow]")

            # Join threads with timeout
            if 'futures_threads' in locals():
                for fm, ft in futures_threads:
                    ft.join(timeout=5)
            elif 'ft' in locals():
                ft.join(timeout=5)
            if 'ot' in locals():
                ot.join(timeout=5)
            if 'st_t' in locals():
                st_t.join(timeout=5)

            # Clear callbacks before logout
            if api is not None:
                try:
                    from core.broker.shioaji_compat import clear_tick_callback, clear_bidask_callback
                    clear_tick_callback(api)
                    time.sleep(0.5)
                    clear_bidask_callback(api)
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
        # Record lifecycle event
        _rec = _lifecycle_recorder
        if _rec is not None:
            _sig_name = {signal.SIGTERM: "SIGTERM", signal.SIGINT: "SIGINT", signal.SIGHUP: "SIGHUP"}.get(signum, f"SIGNAL_{signum}")
            try:
                _rec.set_shutdown_cause(f"SIGNAL_{_sig_name}", "SIGNAL_HANDLER")
                _rec.record_event("SIGNAL_RECEIVED", signal_name=_sig_name, signal_number=signum)
            except Exception:
                pass
        _shutdown_event.set()
        # Give the main loop time to detect the shutdown
        time.sleep(1)
        sys.exit(0)

    # Register signal handlers
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGHUP, signal_handler)
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="Skip broker login entirely")
    # 2026-07-21 Gemini CLI: support custom config names (e.g. futures_mtx)
    parser.add_argument("--config", type=str, default="futures", help="Base config name (e.g. futures_mtx)")
    args = parser.parse_args()
    
    try:
        run_system(dry_run=args.dry_run, config_name=args.config)
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted by user[/yellow]")
        _rec = _lifecycle_recorder
        if _rec is not None:
            try:
                _rec.set_shutdown_cause("SIGNAL_SIGINT", "SIGNAL_HANDLER")
                _rec.record_event("SIGNAL_RECEIVED", signal_name="SIGINT", signal_number=2)
            except Exception:
                pass
        _shutdown_event.set()
        time.sleep(1)
        sys.exit(0)
    except Exception as e:
        console.print(f"[bold red]Unhandled exception in main: {e}[/bold red]")
        _rec = _lifecycle_recorder
        if _rec is not None:
            try:
                _rec.set_shutdown_cause("UNHANDLED_EXCEPTION", "EXCEPTION_HOOK")
                _rec.record_event("SHUTDOWN_BEGIN", shutdown_reason="UNHANDLED_EXCEPTION", shutdown_initiator="EXCEPTION_HOOK",
                                  exception_message=str(e)[:200])
            except Exception:
                pass
        _shutdown_event.set()
        time.sleep(1)
        sys.exit(1)

def ensure_single_instance(config_name="futures"):
    """🛡️ [Pillar 3] Execution Consistency: PID Lock.
    # lock_file = "/tmp/tw_trading_unified.pid"
    # Another main.py instance is running
    # ensure_single_instance()
    """
    import os, psutil
    # 2026-07-21 Gemini CLI: Use distinct PID lock file per config list to allow concurrent running of different configs
    config_names = [c.strip() for c in config_name.split(",") if c.strip()]
    _base_name = "_".join(sorted(config_names)).replace(".yaml", "")
    lock_file = f"/tmp/tw_trading_unified_{_base_name}.pid"
    if os.path.exists(lock_file):
        try:
            with open(lock_file, "r") as f:
                pid = int(f.read().strip())
            if psutil.pid_exists(pid):
                # Check if it's actually a python trading process
                proc = psutil.Process(pid)
                if "python" in proc.name().lower():
                    print(f"🚨 [FATAL] Another main.py instance for {config_name} is running (PID: {pid}). Exiting.")
                    os._exit(1)
        except Exception: pass
    
    with open(lock_file, "w") as f:
        f.write(str(os.getpid()))

if __name__ == "__main__":
    # 2026-07-21 Gemini CLI: Parse arguments early to extract config name for ensure_single_instance
    import argparse
    _early_parser = argparse.ArgumentParser(add_help=False)
    _early_parser.add_argument("--config", type=str, default="futures")
    _early_args, _ = _early_parser.parse_known_args()
    
    ensure_single_instance(_early_args.config)
    main()
