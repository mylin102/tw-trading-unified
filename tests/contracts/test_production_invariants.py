"""Production invariants (I3 FAILPATH + exit-code contract).

2026-11-11 Hermes Agent. Two invariants are locked here:

I3-FAILPATH (scripts/ci/check_no_swallowed_except.py)
    A handler that wraps an order/exit/session operation must record
    evidence or re-raise. Silent `pass` / `continue` around `submit(...)`
    is how a real order failure becomes an invisible one.

EXIT-CODE CONTRACT (main._shutdown_exit_code)
    2026-11-07 and 2026-11-10: contract resolution failed, the process
    exited 0, and PM2's `stop_exit_codes: [0]` treated that as a
    deliberate operator stop -> no restart -> dead for 83 minutes, twice.
    Only an operator stop or a signal may map to exit 0.
"""
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "ci"))

import check_no_swallowed_except as gate  # noqa: E402


# ── I3 FAILPATH: the gate must bite on the shape it exists to catch ──

def _scan(tmp_path: Path):
    return gate.scan_tree(tmp_path)


def test_gate_flags_silent_handler_around_submit(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert found[0]["line"] == 4
    assert "submit" in found[0]["wrapped_call"]


def test_gate_flags_silent_continue_around_release(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f(items):\n"
        "    for i in items:\n"
        "        try:\n"
        "            strat.release(i)\n"
        "        except Exception:\n"
        "            continue\n"
    )
    assert len(_scan(tmp_path)) == 1


def test_gate_flags_bare_except_around_execute_trade(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f():\n"
        "    try:\n"
        "        self._execute_trade('EXIT', 1.0)\n"
        "    except:\n"
        "        pass\n"
    )
    assert len(_scan(tmp_path)) == 1


def test_gate_requires_annotation_even_when_logging(tmp_path):
    """Logging alone is not the standard: the handler must declare whether
    the failure is survivable (agent_engineering_governance I3)."""
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        logger.warning('submit failed', exc_info=True)\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert found[0]["severity"] == "WARN"
    assert found[0]["problem"].startswith("no FAILPATH annotation")


def test_gate_allows_annotated_recoverable_handler(tmp_path):
    (tmp_path / "ok.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception as exc:\n"
        "        # FAILPATH: RECOVERABLE reason=ORDER_STATE_RACE\n"
        "        logger.warning('submit failed: %s', exc)\n"
    )
    assert _scan(tmp_path) == []


def test_gate_flags_annotated_recoverable_without_evidence(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: RECOVERABLE reason=ORDER_STATE_RACE\n"
        "        pass\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "no log/event evidence" in found[0]["problem"]


def test_gate_flags_fatal_annotation_without_raise(tmp_path):
    """Declaring FATAL while silently continuing is the exact contradiction
    the annotation exists to expose."""
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: FATAL reason=POSITION_STATE_UNREADABLE\n"
        "        logger.error('submit failed')\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "does not raise" in found[0]["problem"]


def test_gate_allows_fatal_annotation_with_raise(tmp_path):
    (tmp_path / "ok.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: FATAL reason=POSITION_STATE_UNREADABLE\n"
        "        raise\n"
    )
    assert _scan(tmp_path) == []


def test_gate_flags_unregistered_reason(tmp_path):
    (tmp_path / "bad.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        # FAILPATH: RECOVERABLE reason=MADE_UP_REASON\n"
        "        logger.warning('x')\n"
    )
    found = _scan(tmp_path)
    assert len(found) == 1
    assert "unregistered reason" in found[0]["problem"]


def test_gate_allows_reraise(tmp_path):
    (tmp_path / "ok.py").write_text(
        "def f(order):\n"
        "    try:\n"
        "        broker.submit(order)\n"
        "    except Exception:\n"
        "        raise\n"
    )
    assert _scan(tmp_path) == []


def test_gate_ignores_silent_handler_around_non_order_call(tmp_path):
    """Scoping rule: a silent handler around unrelated IO is noise, not
    an order hazard -- it must NOT be flagged (keeps signal high)."""
    (tmp_path / "fine.py").write_text(
        "def f(obj):\n"
        "    try:\n"
        "        json.dumps(obj)\n"
        "    except Exception:\n"
        "        pass\n"
    )
    assert _scan(tmp_path) == []


# ── EXIT-CODE CONTRACT ──

def test_exit_code_contract():
    from main import _shutdown_exit_code

    # Only an operator stop or a POSIX signal is a clean exit. The signal
    # convention in main.py:2333 is "SIGNAL_<NAME>" (set_shutdown_cause),
    # verified against the live source rather than assumed.
    assert _shutdown_exit_code("SIGNAL_SIGTERM") == 0
    assert _shutdown_exit_code("SIGNAL_SIGINT") == 0
    assert _shutdown_exit_code("SIGNAL_SIGHUP") == 0
    assert _shutdown_exit_code("OPERATOR_STOP") == 0

    # A bare signal name is NOT the contract's spelling -- it must fall
    # through to the non-zero default rather than silently be treated as
    # an operator stop.
    assert _shutdown_exit_code("SIGTERM") != 0

    # Everything else must be visible to the supervisor as a failure.
    for fatal in ("CRITICAL_CRASH", "MAIN_LOOP_RETURNED", "MONITOR_STOPPED",
                  "RECOVERABLE_SESSION_LOSS", "STARTUP_INVARIANT_VIOLATED",
                  "CONTRACT_RESOLUTION_FAILED", "SESSION_LOGOUT"):
        assert _shutdown_exit_code(fatal) != 0, (
            f"{fatal} must not exit 0 -- PM2 stop_exit_codes:[0] would "
            f"treat it as a deliberate operator stop and never restart")


def test_recoverable_session_loss_uses_distinct_code():
    """Distinct non-zero code so operators can tell 'retry may help' from
    'this process must not run'."""
    from main import _shutdown_exit_code
    assert _shutdown_exit_code("RECOVERABLE_SESSION_LOSS") == 75
    assert _shutdown_exit_code("CRITICAL_CRASH") == 1
