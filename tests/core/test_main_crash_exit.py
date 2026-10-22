"""Slice X: a Critical crash in main.run_system must exit non-zero.

Before this, the crash was logged, the cleanup ran, and the process returned
normally (exit 0). ecosystem.config.js lists stop_exit_codes [0], so PM2 read
that as a deliberate stop and did not restart, leaving trading down unnoticed.

Each case runs main in a fresh interpreter: run_system starts monitor threads
and must not leak into the pytest process.
"""
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

HARNESS = textwrap.dedent(
    """
    import sys, threading, time

    sys.path.insert(0, {repo!r})
    import main

    MODE = {mode!r}
    real_sleep = time.sleep
    time.sleep = lambda s: real_sleep(min(s, 0.05))

    if MODE == "crash":
        # Fault injection inside run_system's try body (dry-run has no broker):
        # the monitor constructor raises the way set_tick_callback raised
        # "Already borrowed" on Mini.  Nothing else in the process starts.
        from strategies.futures.monitor import FuturesMonitor

        def _boom(self, *a, **k):
            raise RuntimeError("Already borrowed")

        FuturesMonitor.__init__ = _boom

    def logout_marker(*a, **k):
        print("LOGOUT_CALLED", flush=True)

    main.logout = logout_marker

    if MODE == "keyboard":
        def run_system(**k):
            raise KeyboardInterrupt
        main.run_system = run_system
    elif MODE == "unhandled":
        def run_system(**k):
            raise ValueError("boom before loop")
        main.run_system = run_system

    sys.argv = ["main.py", "--dry-run"]
    main.main()
    print("MAIN_RETURNED", flush=True)
    """
)


def _run(tmp_path, mode):
    script = tmp_path / f"harness_{mode}.py"
    script.write_text(HARNESS.format(repo=str(REPO), mode=mode), encoding="utf-8")
    env = dict(os.environ, TRADING_RUNTIME_DIR=str(tmp_path / "runtime"))
    return subprocess.run([sys.executable, str(script)], cwd=str(tmp_path),
                          env=env, capture_output=True, text=True, timeout=240)


def test_shutdown_exit_code_maps_critical_crash_to_nonzero():
    sys.path.insert(0, str(REPO))
    import main
    assert main._shutdown_exit_code("CRITICAL_CRASH") != 0


def test_critical_crash_exits_nonzero_and_cleans_up(tmp_path):
    proc = _run(tmp_path, "crash")
    out = proc.stdout + proc.stderr
    assert "Critical crash" in out, out[-2000:]
    assert "Exited main loop" in out, out[-2000:]
    assert "LOGOUT_CALLED" in out, out[-2000:]
    assert "MAIN_RETURNED" not in out, out[-2000:]
    assert proc.returncode != 0, out[-2000:]


def test_keyboard_interrupt_still_exits_zero(tmp_path):
    proc = _run(tmp_path, "keyboard")
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]


def test_unhandled_exception_still_exits_one(tmp_path):
    proc = _run(tmp_path, "unhandled")
    assert proc.returncode == 1, (proc.stdout + proc.stderr)[-2000:]


@pytest.mark.parametrize("reason", ["SIGNAL_SIGTERM", "OPERATOR_STOP"])
def test_normal_shutdown_reasons_still_exit_zero(reason):
    sys.path.insert(0, str(REPO))
    import main
    assert main._shutdown_exit_code(reason) == 0
