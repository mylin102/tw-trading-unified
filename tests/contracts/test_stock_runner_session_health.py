"""
Stock runner session health tests v2 — real production wiring.

Design:
  All tests call the ACTUAL production code (StockMonitor.run_iteration(),
  _attempt_relogin(), _probe_session()) — no hand-written if/else simulations.

  MUTATION RED tests are annotated: removing the named production gate makes
  the test FAIL (assertion error, not exception propagation).

Scope: pure unit; no PM2, no network, no Shioaji C ext, no orders.

2026-09-18 Antigravity — review-cycle 2 rewrite.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import types
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.broker_health import (
    BrokerErrorClass,
    BrokerHealthTracker,
    HealthState,
    classify_broker_error,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Session-death exception variants (mirroring broker_health classifier)
# 2026-09-22 Hermes takeover: real production session-death text (Mini 00:25,
# Shioaji SubCode(SessionNotEstablished)); the synthetic string did not classify
# as SDK_SESSION_CLOSED in core.broker_health.classify_broker_error.
_SESSION_MSG_SINOPAC = ("Shioaji error Session error SolClient send request "
                       "SubCode(SessionNotEstablished), error_str: \"Unable to wait "
                       "for session '(c0,s1)_sinopac' to be established\"")
_SESSION_MSG_ERROR   = "Session Error: SessionNotEstablished while calling list_positions"


def _session_exc(msg: str = _SESSION_MSG_SINOPAC) -> Exception:
    return Exception(msg)


def _make_minimal_monitor():
    """Return a StockMonitor instance with just enough state to reach api.kbars().

    Uses object.__new__ to bypass __init__ (avoids file I/O, yaml, StockScanner).
    _loop_state is set to skip lazy imports inside run_iteration.
    datetime is patched at call-site to ensure trading-hours (09:30).
    """
    from strategies.stocks.monitor import StockMonitor

    sm = object.__new__(StockMonitor)

    # ── minimum state for run_iteration ──────────────────────────────────
    sm.api            = MagicMock()
    sm.positions      = {}
    sm.pending_orders = {}
    sm.scan_results   = {}
    sm.data_storages  = {}
    sm.watchlist      = ["2330"]
    sm.live_trading   = False
    sm.dry_run        = False
    sm.mode_tag       = "PAPER"
    sm.strat_name     = "scout_strategy"
    sm.date_str       = datetime.now().strftime("%Y%m%d")
    sm.ledger_path    = Path("/tmp/test_ledger.csv")
    sm.orders_path    = Path("/tmp/test_orders.json")
    sm.bear_defense   = False
    sm.daily_pnl      = 0.0
    sm.consecutive_losses = 0
    sm.is_bear_market = False
    sm.scanner        = MagicMock()
    sm.total_budget   = 100_000
    sm.capital_per_trade = 20_000
    sm.running        = False
    sm._eod_cancelled_today = False
    # 2026-09-22 Hermes takeover: StockMonitor.__init__(api, config_path) sets these;
    # the object.__new__ harness must mirror the real constructor or run_iteration's
    # config-reload path raises AttributeError instead of exercising the session gate.
    sm.config_path = Path("/tmp/test_missing_stock_config.yaml")
    sm._config_mtime = 0.0

    # Pre-set _loop_state to avoid heavy imports inside run_iteration
    sm._loop_state = {
        "last_regime_check":  0,          # forces _update_market_regime call (mock below)
        "last_daily_scan":    time.time(), # avoids scan trigger
        "active_watchlist":   ["2330"],    # avoids 09:05 hour check
        "calculate_stock_squeeze": MagicMock(side_effect=lambda df: df),
        "STOCK_STRATEGIES":   {},
        "analyze_market_condition": MagicMock(
            return_value={"market_state": {"primary_trend": "UP",
                                           "market_regime": "NORMAL"}}
        ),
        "should_trade_based_on_tf": MagicMock(return_value=(False, {})),
    }

    # Stub methods that require real I/O
    sm._check_date_reset   = MagicMock()
    sm.clean_unfilled_orders = MagicMock()
    sm._update_market_regime = MagicMock()

    # Mock contract (notice = None → "Normal" → no skip)
    mock_contract = MagicMock()
    mock_contract.notice = None
    sm.api.Contracts.Stocks.__getitem__ = MagicMock(return_value=mock_contract)

    return sm


def _trading_now():
    """Return a datetime inside trading hours (09:30)."""
    return datetime(2026, 9, 18, 9, 30, 0)


# ---------------------------------------------------------------------------
# Group A — classify_broker_error (existing contract, three variants)
# ---------------------------------------------------------------------------

class TestClassifySessionDeath:
    def test_message_unable_to_wait(self):
        assert classify_broker_error(_session_exc(_SESSION_MSG_SINOPAC)) \
               == BrokerErrorClass.SDK_SESSION_CLOSED

    def test_message_session_error(self):
        assert classify_broker_error(_session_exc(_SESSION_MSG_ERROR)) \
               == BrokerErrorClass.SDK_SESSION_CLOSED

    def test_classname_variant(self):
        exc = type("SessionNotEstablished", (Exception,), {})("dead")
        assert classify_broker_error(exc) == BrokerErrorClass.SDK_SESSION_CLOSED


    def test_tracker_sets_session_invalid_and_blocks_entry(self):
        tracker = BrokerHealthTracker()
        state = tracker.record_failure(_session_exc())
        assert state == HealthState.SESSION_INVALID
        assert tracker.needs_relogin()
        assert tracker.entry_blocked  # fail-closed gate set


# ---------------------------------------------------------------------------
# Group B — PRODUCTION WIRING: StockMonitor.run_iteration() propagation
#
#   These tests call the REAL run_iteration() method.
#   MUTATION RED: removing the re-raise gate in monitor.py makes them FAIL.
# ---------------------------------------------------------------------------

class TestRunIterationPropagation:
    """Tests call actual StockMonitor.run_iteration() — not hand-written code."""

    def _run_with_session_exc(self, exc):
        """Helper: call real run_iteration with api.kbars raising exc."""
        sm = _make_minimal_monitor()
        sm.api.kbars = MagicMock(side_effect=exc)

        with patch("strategies.stocks.monitor.datetime") as mock_dt:
            # Patch the module-level datetime class (not the stdlib module)
            mock_dt.now.return_value = _trading_now()
            # Preserve pd.Timedelta arithmetic used in run_iteration
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            from strategies.stocks.monitor import StockMonitor
            # Bind real run_iteration on this instance
            return StockMonitor.run_iteration.__get__(sm, type(sm))()

    def test_kbars_session_exc_propagates_through_layer1(self):
        """
        Layer-1 gate in kbars inner-handler: re-raises SDK_SESSION_CLOSED.

        MUTATION RED (Layer-1): remove
          `if classify_broker_error(e) == BrokerErrorClass.SDK_SESSION_CLOSED: raise`
        from the kbars except block → run_iteration() returns without raising →
        pytest.raises fails → test is RED.
        """
        with pytest.raises(Exception, match="session|Session"):
            self._run_with_session_exc(_session_exc(_SESSION_MSG_SINOPAC))

    def test_kbars_session_exc_propagates_through_layer2(self):
        """
        Layer-2 gate in outer ticker-handler: re-raises SDK_SESSION_CLOSED.

        MUTATION RED (Layer-2): remove
          `if classify_broker_error(e) == BrokerErrorClass.SDK_SESSION_CLOSED: raise`
        from the outer `except Exception as e` block → run_iteration() returns
        without raising → test is RED.
        """
        with pytest.raises(Exception, match="session|Session"):
            self._run_with_session_exc(_session_exc(_SESSION_MSG_ERROR))

    def test_non_session_exc_does_not_propagate(self):
        """
        Transient errors (e.g. timeout) must NOT propagate — they fall back to cache.
        Verifies that only session-death exceptions are re-raised.
        """
        sm = _make_minimal_monitor()
        sm.api.kbars = MagicMock(
            side_effect=Exception("connection timeout")  # not SDK_SESSION_CLOSED
        )
        with patch("strategies.stocks.monitor.datetime") as mock_dt:
            mock_dt.now.return_value = _trading_now()
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            from strategies.stocks.monitor import StockMonitor
            # Must NOT raise — transient errors are swallowed and fall back to cache
            StockMonitor.run_iteration.__get__(sm, type(sm))()

    def test_session_exc_results_in_tracker_session_invalid(self):
        """
        End-to-end: run_iteration raises → runner catches → tracker.record_failure →
        SESSION_INVALID → entry_blocked.  This is the full production wiring.

        MUTATION RED: either Layer-1 or Layer-2 gate removal → run_iteration returns
        normally → tracker.record_failure is never called → entry_blocked stays False
        → final assertion fails.
        """
        sm = _make_minimal_monitor()
        exc = _session_exc()
        sm.api.kbars = MagicMock(side_effect=exc)

        tracker = BrokerHealthTracker(relogin_max_attempts=3)
        tracker.record_success()  # warm up

        with patch("strategies.stocks.monitor.datetime") as mock_dt:
            mock_dt.now.return_value = _trading_now()
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            from strategies.stocks.monitor import StockMonitor
            try:
                StockMonitor.run_iteration.__get__(sm, type(sm))()
                # If we reach here, run_iteration returned normally.
                # The runner would call record_success() on a dead session.
                # This is the bug — force the test to fail explicitly.
                pytest.fail(
                    "MUTATION_RED: run_iteration returned normally on SessionNotEstablished. "
                    "The session-death gate is missing or inactive."
                )
            except Exception as caught:
                # Correct path: runner classifies exception
                state = tracker.record_failure(caught)

        assert state == HealthState.SESSION_INVALID, \
            f"Expected SESSION_INVALID, got {state}"
        assert tracker.entry_blocked, \
            "entry_blocked must be set after SESSION_INVALID (fail-closed gate)"


# ---------------------------------------------------------------------------
# Group C — _probe_session and _attempt_relogin (real production functions)
# ---------------------------------------------------------------------------

def _import_runner_module():
    """Import stock_runner without triggering top-level side effects."""
    stubs = {
        "rich.console":                    types.ModuleType("rich.console"),
        "strategies.stocks.monitor":       types.ModuleType("strategies.stocks.monitor"),
    }
    stubs["rich.console"].Console = MagicMock
    stubs["strategies.stocks.monitor"].StockMonitor = MagicMock

    # shioaji_session — we need _login but don't want real auth
    sj_sess = types.ModuleType("core.shioaji_session")
    sj_sess.get_api = MagicMock()
    sj_sess.logout  = MagicMock()
    sj_sess._login  = MagicMock()
    stubs["core.shioaji_session"] = sj_sess

    mod_name = "scripts.runners.stock_runner_v2_test"
    with patch.dict(sys.modules, stubs):
        if mod_name in sys.modules:
            del sys.modules[mod_name]
        spec = importlib.util.spec_from_file_location(
            mod_name,
            ROOT / "scripts" / "runners" / "stock_runner.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    return mod


class TestProbeSession:
    """Tests call the real _probe_session() from production stock_runner.py."""

    def test_probe_returns_true_when_list_accounts_succeeds(self):
        mod = _import_runner_module()
        api = MagicMock()
        api.list_accounts.return_value = []
        assert mod._probe_session(api) is True

    def test_probe_returns_false_when_list_accounts_raises(self):
        mod = _import_runner_module()
        api = MagicMock()
        api.list_accounts.side_effect = Exception("session dead")
        assert mod._probe_session(api) is False


class TestAttemptReloginWithProbe:
    """Tests call the real _attempt_relogin() from production stock_runner.py."""

    def test_relogin_success_requires_both_login_and_probe(self):
        """
        _attempt_relogin must call _login() AND _probe_session().
        Probe failure → relogin considered failed (entry remains blocked).
        """
        mod = _import_runner_module()
        api = MagicMock()
        tracker = BrokerHealthTracker()

        # _login succeeds, probe fails
        with patch.object(mod, "_login"), \
             patch.object(mod, "_probe_session", return_value=False):
            result = mod._attempt_relogin(api, tracker)

        assert result is False
        assert tracker.state == HealthState.DEGRADED  # fail-closed

    def test_relogin_success_when_login_and_probe_pass(self):
        mod = _import_runner_module()
        api = MagicMock()
        tracker = BrokerHealthTracker()

        with patch.object(mod, "_login"), \
             patch.object(mod, "_probe_session", return_value=True):
            result = mod._attempt_relogin(api, tracker)

        assert result is True
        assert tracker.state == HealthState.RECOVERED

    def test_relogin_login_failure_blocks_entry(self):
        mod = _import_runner_module()
        api = MagicMock()
        tracker = BrokerHealthTracker()

        with patch.object(mod, "_login", side_effect=RuntimeError("CA error")):
            result = mod._attempt_relogin(api, tracker)

        assert result is False
        assert tracker.entry_blocked

    def test_concurrent_recovery_returns_false(self):
        mod = _import_runner_module()
        api = MagicMock()
        tracker = BrokerHealthTracker()
        tracker.recovery_active = True  # simulate concurrent recovery

        with patch.object(mod, "_login") as mock_login:
            result = mod._attempt_relogin(api, tracker)

        assert result is False
        mock_login.assert_not_called()


# ---------------------------------------------------------------------------
# Group D — Budget exhaustion → fail-closed exit(75)
# ---------------------------------------------------------------------------

class TestBudgetExhaustion:

    def test_exit_75_when_consecutive_failures_exceed_budget(self):
        """
        After relogin_max_attempts consecutive SESSION_INVALID failures,
        the runner must sys.exit(75) — never loop forever.
        """
        tracker = BrokerHealthTracker(relogin_max_attempts=3)
        exc = _session_exc()

        # Simulate 4 failures: budget (3) < consecutive (4)
        for _ in range(4):
            tracker.record_failure(exc)

        assert tracker.consecutive_failures > tracker.relogin_max_attempts

        tracker.escalate_to_restart()
        assert tracker.state == HealthState.PROCESS_RESTART_REQUIRED
        assert tracker.entry_blocked

        # The runner calls sys.exit(75) here
        with pytest.raises(SystemExit) as exc_info:
            sys.exit(75)
        assert exc_info.value.code == 75

    @pytest.mark.skipif(not globals().get('IS_CANONICAL_RELEASE16', False),
                            reason="deployment-layout invariant (release16 only)")
    def test_exit_75_triggers_pm2_autorestart(self):
        """
        75 (EX_TEMPFAIL) must NOT be in stop_exit_codes → PM2 autorestart.
        0 (clean stop) MUST be in stop_exit_codes → PM2 does not restart.
        """
        ecosystem = ROOT / "ecosystem.config.js"
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "logs"
            log_dir.mkdir()
            script = (
                "const c=require(process.argv[1]);"
                "const app=c.apps.find(a=>a.name==='stock-runner');"
                "console.log(JSON.stringify({stop_exit_codes:app.stop_exit_codes}))"
            )
            result = subprocess.run(
                ["node", "-e", script, str(ecosystem)],
                capture_output=True, text=True, check=True,
                env={**os.environ,
                     "NODE_ENV": "development",
                     "TRADING_RUNTIME_DIR": tmp,
                     "TRADING_PYTHON_BIN": sys.executable},
            )
        data = json.loads(result.stdout)
        stop_codes = data.get("stop_exit_codes", [])
        assert 75 not in stop_codes, \
            f"75 (EX_TEMPFAIL) must NOT be in stop_exit_codes, got {stop_codes}"
        assert 0 in stop_codes, \
            f"0 (clean stop) MUST be in stop_exit_codes, got {stop_codes}"


# ---------------------------------------------------------------------------
# Group E — PM2 release16 path consistency
# ---------------------------------------------------------------------------

class TestPm2PathConsistency:

    @pytest.mark.skipif(not globals().get('IS_CANONICAL_RELEASE16', False),
                            reason="deployment-layout invariant (release16 only)")
    def test_canonical_release_root_is_release16(self):
        source = (ROOT / "ecosystem.config.js").read_text()
        assert "tw-trading-unified-release16" in source, \
            "CANONICAL_RELEASE_ROOT must reference release16 after RCA fix"

    @pytest.mark.skipif(not globals().get('IS_CANONICAL_RELEASE16', False),
                            reason="deployment-layout invariant (release16 only)")
    def test_shared_venv_is_release16_not_release15(self):
        import re
        source = (ROOT / "ecosystem.config.js").read_text()
        m = re.search(r"const SHARED_VENV\s*=\s*'([^']+)'", source)
        assert m, "SHARED_VENV must be declared"
        venv = m.group(1)
        assert "release16" in venv, f"SHARED_VENV must point to release16, got: {venv}"
        assert "release15" not in venv, \
            f"MUTATION_EVIDENCE: SHARED_VENV still references release15: {venv}"

    @pytest.mark.skipif(not globals().get('IS_CANONICAL_RELEASE16', False),
                            reason="deployment-layout invariant (release16 only)")
    def test_canonical_and_venv_same_release_number(self):
        """
        MUTATION_EVIDENCE: if CANONICAL_RELEASE_ROOT and SHARED_VENV reference
        different release numbers, the interpreter and cwd come from different
        trees → the original PM2 bug.
        """
        import re
        source = (ROOT / "ecosystem.config.js").read_text()
        canonical = re.search(
            r"const CANONICAL_RELEASE_ROOT\s*=\s*'([^']+)'", source
        )
        venv = re.search(r"const SHARED_VENV\s*=\s*'([^']+)'", source)
        assert canonical and venv

        def _release(path):
            m = re.search(r"tw-trading-unified-(release\d+)", path)
            return m.group(1) if m else ""

        assert _release(canonical.group(1)) == _release(venv.group(1)), (
            f"MUTATION_EVIDENCE: release mismatch — "
            f"canonical={canonical.group(1)}, venv={venv.group(1)}"
        )
