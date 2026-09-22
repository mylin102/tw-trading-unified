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
import logging
from pathlib import Path
from rich.console import Console

# Add project root to path
BASE = Path(__file__).parent.parent.parent
sys.path.insert(0, str(BASE))

from core.shioaji_session import get_api, logout, _login  # _login: relogin needs direct re-auth call
from core.broker_health import BrokerHealthTracker, HealthState
from strategies.stocks.monitor import StockMonitor

console = Console()
logger = logging.getLogger(__name__)

# ── GSD: Singleton Lock ─────────────────────────────────────────────
LOCKFILE = "/tmp/stock_runner_singleton.lock"

# ── Session health constants ─────────────────────────────────────────
# Exit code 75 = EX_TEMPFAIL: recoverable temporary failure.
# PM2 stop_exit_codes=[0] → 75 triggers PM2 autorestart (fresh session).
_EXIT_RECOVERABLE = 75


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
        except Exception:
            console.print("[bold red]🔒 Another stock_runner is already running. Exiting.[/bold red]")
        sys.exit(1)


def _attempt_relogin(api, tracker: BrokerHealthTracker) -> bool:
    """Try to re-establish a dead Shioaji session (fail-closed design).

    Uses the existing _login() function from core.shioaji_session — no new
    logic, just wiring.  Returns True if relogin + probe succeeded, False otherwise.
    Caller must NOT place any orders until this returns True.
    """
    gen = tracker.begin_recovery()
    if gen is None:
        # Another recovery is already in progress (should not happen in
        # single-threaded runner, but guard defensively).
        console.print("[yellow]⚠️ Recovery already in progress, skipping duplicate attempt.[/yellow]")
        return False

    console.print(f"[bold yellow]🔄 Session dead — attempting relogin (generation={gen})...[/yellow]")
    try:
        # Step A: re-authenticate the existing api object in-place.
        _login(api)
        # Step B: post-relogin probe — verify session is truly usable, not
        # just "login call didn't raise".  A successful _login() result does
        # not guarantee the transport is alive (e.g. CA error after login).
        if not _probe_session(api):
            raise RuntimeError("Post-relogin probe failed: session still dead after _login()")
        tracker.finish_recovery(gen, ok=True)
        console.print("[bold green]✅ Session re-established and probe passed.[/bold green]")
        return True
    except Exception as exc:
        console.print(f"[bold red]❌ Relogin/probe failed: {exc}[/bold red]")
        tracker.finish_recovery(gen, ok=False)
        return False


def _probe_session(api) -> bool:
    """Lightweight read-only probe to verify session liveness post-relogin.

    Calls api.list_accounts() — pure read, no subscriptions, no orders.
    Returns True if the session responds; False if still dead.

    Design: this prevents record_success() being called on a session where
    _login() returned without error but the underlying transport is still dead.
    """
    try:
        _ = api.list_accounts()
        return True
    except Exception:
        return False


def run_stock_monitor(dry_run=False):
    api = None
    lock_fd = check_singleton()

    # Instantiate the existing health tracker (梯級 2: reuse, don't rewrite).
    tracker = BrokerHealthTracker(relogin_max_attempts=3)

    try:
        console.print("[bold green]🍎 Starting Standalone Stock Monitor...[/bold green]")

        # 1. Login
        api = get_api()
        console.print("[green]✅ Shioaji session established for Stocks[/green]")
        tracker.record_success()  # warm up the healthy state

        # 2. Initialize Monitor
        sm = StockMonitor(
            api=api,
            config_path=os.path.join(BASE, "config", "stocks.yaml"),
            dry_run=dry_run
        )

        # [GSD Fix] 啟動時執行標準設置（包含恢復部位與保存狀態）
        sm.setup()
        sm._save_orders_file()

        # 3. Execution Loop — now with session health detection
        while True:
            try:
                # Block entry if session is in a non-HEALTHY / non-RECOVERED state.
                # This is the fail-closed gate: no orders while session is unknown.
                if tracker.entry_blocked:
                    console.print(
                        f"[bold yellow]⛔ Entry blocked (health={tracker.state.value}), "
                        "skipping iteration.[/bold yellow]"
                    )
                    time.sleep(5)
                    continue

                sm.run_iteration()
                # Successful iteration: tell the tracker the session is alive.
                tracker.record_success()

            except Exception as e:
                # ── Session health classification (梯級 2: existing classifier) ──
                health_state = tracker.record_failure(e)
                console.print(
                    f"[bold red]Error in run_iteration: {e} "
                    f"[health={health_state.value}, "
                    f"class={tracker.last_error_class.value if tracker.last_error_class else 'UNKNOWN'}]"
                    "[/bold red]"
                )
                import traceback
                console.print(traceback.format_exc())

                # ── Session death: fail-closed relogin path ──────────────────
                if health_state == HealthState.SESSION_INVALID:
                    # tracker.relogin_max_attempts guards total attempts via
                    # tracker.consecutive_failures (already incremented above).
                    if tracker.consecutive_failures <= tracker.relogin_max_attempts:
                        ok = _attempt_relogin(api, tracker)
                        if ok:
                            # Session alive again; clear error state and resume.
                            tracker.record_success()
                            continue
                        # Relogin failed; tracker.state is now DEGRADED.
                        # Stay in the loop: next iteration will hit entry_blocked.

                    else:
                        # Exhausted relogin budget — escalate to process restart.
                        tracker.escalate_to_restart()
                        console.print(
                            f"[bold red]💀 Relogin budget exhausted "
                            f"({tracker.relogin_max_attempts} attempts). "
                            f"Exiting with code {_EXIT_RECOVERABLE} for PM2 autorestart.[/bold red]"
                        )
                        sys.exit(_EXIT_RECOVERABLE)

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
