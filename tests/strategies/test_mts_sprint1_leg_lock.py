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

def test_restart_rebind_matches_broker_identity(tmp_path):
    """Codex #1: after a restart the session_generation changes — the lock
    must still match via broker identity (trade_id+contract+side+qty) and
    rebind to the new generation."""
    mon = _monitor(tmp_path)
    k1 = _lock_key(session_generation="gen-OLD-1")
    assert mon._leg_lock_acquire(k1) is True
    # new process, NEW generation, same trade/contract/side/qty
    mon2 = _monitor(tmp_path)
    k2 = _lock_key(session_generation="gen-NEW-2")
    assert mon2._leg_lock_check(k2) is True
    # the lock was rebound to the new generation
    with open(mon2._leg_lock_path(), encoding="utf-8") as f:
        d = json.load(f)
    assert any(v.get("session_generation") == "gen-NEW-2"
               for v in d.values())


def test_corrupted_lock_file_fails_safe(tmp_path):
    mon = _monitor(tmp_path)
    _p = mon._leg_lock_path()
    with open(_p, "w", encoding="utf-8") as f:
        f.write("{ not json !!!")
    k = _lock_key()
    assert mon._leg_lock_check(k) is False  # fail-safe: no lock assumed
    assert mon._leg_lock_acquire(k) is True  # recover and write fresh


def test_pending_unconfirmed_never_released_early(tmp_path):
    """Codex #3: PENDING_UNCONFIRMED must NOT be released — only terminal
    states or SUBMIT_FAILED release."""
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k, status="PENDING_UNCONFIRMED") is True
    mon._leg_lock_release(k)  # must NOT release a pending lock
    assert mon._leg_lock_check(k) is True


def test_submit_failed_releases_lock(tmp_path):
    """Codex #3: SUBMIT_FAILED releases the lock so a later signal can try."""
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k, status="SUBMIT_FAILED") is True
    mon._leg_lock_release(k)
    assert mon._leg_lock_check(k) is False


def test_terminal_status_releases_lock(tmp_path):
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k, status="PENDING_UNCONFIRMED") is True
    mon._leg_lock_release(k)  # still pending -> not released
    assert mon._leg_lock_check(k) is True
    # broker terminal: mark FILLED and release
    with open(mon._leg_lock_path(), encoding="utf-8") as f:
        d = json.load(f)
    for v in d.values():
        v["status"] = "FILLED"
    mon._leg_lock_save(d)
    mon._leg_lock_release(k)
    assert mon._leg_lock_check(k) is False


def test_concurrent_acquire_last_writer_wins_no_corruption(tmp_path):
    """Two acquires on the same leg must not corrupt the file; the final
    state is one lock for that leg."""
    m1 = _monitor(tmp_path)
    m2 = _monitor(tmp_path)
    k = _lock_key()
    assert m1._leg_lock_acquire(k) is True
    assert m2._leg_lock_acquire(k) is True
    with open(m1._leg_lock_path(), encoding="utf-8") as f:
        d = json.load(f)
    assert len(d) == 1  # one lock entry, not duplicated/corrupted

