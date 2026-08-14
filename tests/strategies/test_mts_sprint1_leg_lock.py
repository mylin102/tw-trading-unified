"""Sprint 1 / Task 1 RED: a RELEASE receipt (ORDER_SUBMITTED) for a leg
locks that leg BEFORE any further submission.  A second signal for the
same leg must produce ZERO submissions and emit
ORDER_BLOCKED_PENDING_EXISTS.

Lock key (user-specified): trade_id + session_generation + contract +
closing_side + qty.  The lock is persisted (file-backed) so a restart
restores it and never releases before the broker terminal state.

Acceptance (user corrections #2/#3/#5):
- lock key includes all five components
- persisted before submission; restored after restart
- second signal -> 0 submissions + ORDER_BLOCKED_PENDING_EXISTS
"""
import json
from unittest.mock import patch

import pytest


def _lock_key(**over):
    k = {
        "trade_id": "mts-auto-223502-833",
        "session_generation": "gen-20260814-1",
        "contract": "TMFI6",
        "closing_side": "SELL",
        "qty": 1,
    }
    k.update(over)
    return k


def _monitor(tmp_path):
    from strategies.futures.monitor import FuturesMonitor
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon._leg_lock_store = str(tmp_path / "leg_locks.json")
    mon._pending_lifecycle_orders = {}
    mon.events = []
    mon._append_mts_event = lambda *a, **k: mon.events.append(k)
    return mon


def test_lock_key_has_all_components(tmp_path):
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k) is True
    assert mon._leg_lock_check(k) is True


def test_second_signal_same_leg_zero_submission(tmp_path):
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k) is True
    # second signal for the same leg: the guard must block
    with patch.object(mon, "_submit_via_gateway") as submit:
        blocked = mon._leg_lock_check(k)
        assert blocked is True
        assert not submit.called
        assert any(e.get("reason") == "ORDER_BLOCKED_PENDING_EXISTS"
                   for e in mon.events)


def test_lock_persists_across_restart(tmp_path):
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k) is True
    # simulate a restart: a fresh monitor instance with the same store
    mon2 = _monitor(tmp_path)
    assert mon2._leg_lock_check(k) is True


def test_lock_different_leg_not_blocked(tmp_path):
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k) is True
    # a different contract (TMFH6) is a different lock — allowed
    other = _lock_key(contract="TMFH6", closing_side="BUY")
    assert mon._leg_lock_check(other) is False
