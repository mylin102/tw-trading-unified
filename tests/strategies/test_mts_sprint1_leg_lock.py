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

def test_rebind_uses_broker_identity_unique(tmp_path):
    """Codex #1: with a broker_order_id, rebinding must match by broker
    identity (unique), not by the generic 4 fields."""
    mon = _monitor(tmp_path)
    k1 = _lock_key(session_generation="gen-OLD-1", broker_order_id="ORD-A")
    assert mon._leg_lock_acquire(k1) is True
    # two candidate locks share trade/contract/side/qty but differ in id
    k2 = _lock_key(session_generation="gen-OLD-2", broker_order_id="ORD-B")
    assert mon._leg_lock_acquire(k2) is True
    mon2 = _monitor(tmp_path)
    k3 = _lock_key(session_generation="gen-NEW-3", broker_order_id="ORD-B")
    # unique via broker identity -> blocked (rebound to ORD-B lock)
    assert mon2._leg_lock_check(k3) is True


def test_rebind_ambiguous_quarantines(tmp_path):
    """Codex #1: no broker_order_id + multiple candidates -> fail-closed
    QUARANTINE (LEG_LOCK_REBIND_AMBIGUOUS), never a silent pass."""
    mon = _monitor(tmp_path)
    k1 = _lock_key(session_generation="gen-OLD-1", broker_order_id="ORD-A")
    k2 = _lock_key(session_generation="gen-OLD-2", broker_order_id="ORD-B")
    assert mon._leg_lock_acquire(k1) is True
    assert mon._leg_lock_acquire(k2) is True
    mon2 = _monitor(tmp_path)
    k3 = _lock_key(session_generation="gen-NEW-3", broker_order_id="")
    assert mon2._leg_lock_check(k3) is True  # fail-closed: still blocked
    assert any(e.get("reason") == "LEG_LOCK_REBIND_AMBIGUOUS"
               for e in mon2.events)

def test_concurrent_writers_no_lost_update(tmp_path):
    """Codex #2: two writers racing on the lock file must never lose a
    lock — read-modify-write must be serialized (writer flock), and the
    final file must contain BOTH locks."""
    import threading
    mon = _monitor(tmp_path)
    results = []
    barrier = threading.Barrier(2)

    def _w(idx):
        k = _lock_key(contract=f"TMF{idx}H6", closing_side="SELL",
                      broker_order_id=f"ORD-{idx}", seqno=str(idx))
        barrier.wait()
        results.append(mon._leg_lock_acquire(k))

    t1 = threading.Thread(target=_w, args=(1,))
    t2 = threading.Thread(target=_w, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert all(results)
    with open(mon._leg_lock_path(), encoding="utf-8") as f:
        d = json.load(f)
    assert len(d) == 2  # both locks present — no lost update

def test_stable_flock_file_exists(tmp_path):
    """Codex inode-race: the flock must target a stable, never-replaced
    .lock file (the JSON gets os.replace'd — its inode changes)."""
    import os
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k) is True
    assert os.path.exists(mon._leg_lock_flock_path())
    # the flock file is separate from the JSON
    assert mon._leg_lock_flock_path() != mon._leg_lock_path()


def test_race_during_replace_no_lost_lock(tmp_path):
    """Two writers racing while the JSON is atomically replaced must never
    lose a lock — the flock lives on the stable .lock file."""
    import threading
    mon = _monitor(tmp_path)
    results = []
    barrier = threading.Barrier(2)

    def _w(idx):
        k = _lock_key(contract=f"TMF{idx}H6", closing_side="SELL",
                      broker_order_id=f"ORD-{idx}", seqno=str(idx))
        barrier.wait()
        results.append(mon._leg_lock_acquire(k))

    t1 = threading.Thread(target=_w, args=(1,))
    t2 = threading.Thread(target=_w, args=(2,))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert all(results)
    with open(mon._leg_lock_path(), encoding="utf-8") as f:
        d = json.load(f)
    assert len(d) == 2  # both locks present — no lost update

def test_pair_all_or_none_both_locked(tmp_path):
    """③ combined all-or-none: BOTH leg locks acquired together."""
    mon = _monitor(tmp_path)
    k_near = _lock_key(contract="TMFH6", closing_side="BUY",
                       broker_order_id="ORD-N", seqno="1")
    k_far = _lock_key(contract="TMFI6", closing_side="SELL",
                      broker_order_id="ORD-F", seqno="2")
    assert mon._leg_lock_acquire_pair(k_near, k_far) is True
    assert mon._leg_lock_check(k_near) is True
    assert mon._leg_lock_check(k_far) is True


def test_pair_all_or_none_second_conflict_rolls_back(tmp_path):
    """③ if EITHER leg already holds a non-terminal lock, the pair must
    NOT acquire — the other leg must not be left locked, and a
    quarantine/reconcile intent is recorded."""
    mon = _monitor(tmp_path)
    k_near = _lock_key(contract="TMFH6", closing_side="BUY",
                       broker_order_id="ORD-N", seqno="1")
    k_far = _lock_key(contract="TMFI6", closing_side="SELL",
                      broker_order_id="ORD-F", seqno="2")
    # the far leg is already locked (pending release from another signal)
    mon2 = _monitor(tmp_path)
    assert mon2._leg_lock_acquire(k_far) is True
    assert mon._leg_lock_acquire_pair(k_near, k_far) is False
    # the near leg must NOT be locked (all-or-none rollback)
    assert mon._leg_lock_check(k_near) is False
    # quarantine / reconcile intent recorded
    assert any(e.get("reason") == "LEG_LOCK_PAIR_PARTIAL"
               for e in mon.events)

def test_partial_fill_retains_lock(tmp_path):
    """④ a partial fill must NOT release the lock — only a FULL fill is
    terminal; release and resend stay forbidden."""
    mon = _monitor(tmp_path)
    k = _lock_key(qty=1)
    assert mon._leg_lock_acquire(k) is True
    assert mon._leg_lock_apply_broker_deal(k, filled_qty=0.5) is False
    assert mon._leg_lock_check(k) is True  # still locked
    assert any(e.get("reason") == "LEG_LOCK_PARTIAL_FILL"
               for e in mon.events)


def test_empty_query_retains_lock(tmp_path):
    """④ an empty list_trades result (no terminal proof) must NOT release
    the lock."""
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k) is True
    assert mon._leg_lock_apply_broker_query(k, trades=[]) is False
    assert mon._leg_lock_check(k) is True


def test_query_exception_retains_lock(tmp_path):
    """④ a query exception must NOT release the lock — fail-closed."""
    mon = _monitor(tmp_path)
    k = _lock_key()
    assert mon._leg_lock_acquire(k) is True
    assert mon._leg_lock_apply_broker_query(k, trades=None) is False
    assert mon._leg_lock_check(k) is True


def test_full_fill_releases_lock(tmp_path):
    """④ ONLY a full fill (filled_qty >= qty) is terminal -> releases."""
    mon = _monitor(tmp_path)
    k = _lock_key(qty=1)
    assert mon._leg_lock_acquire(k) is True
    assert mon._leg_lock_apply_broker_deal(k, filled_qty=1.0) is True
    assert mon._leg_lock_check(k) is False

def test_partial_submission_quarantine_wiring(tmp_path):
    """Wiring test (user spec): first leg receipt OK + second leg submit
    FAILS -> MTS_ENTRY_RECONCILE / partial-submission quarantine.

    - first leg keeps its lock
    - NO successful second-leg ORDER_SUBMITTED
    - NO auto retry / cancel / compensating order
    - durable reconcile intent restores after restart
    - ambiguous broker query still retains the first-leg lock
    """
    mon = _monitor(tmp_path)
    k_near = _lock_key(contract="TMFH6", closing_side="BUY",
                       broker_order_id="ORD-N", seqno="1")
    k_far = _lock_key(contract="TMFI6", closing_side="SELL",
                      broker_order_id="ORD-F", seqno="2")
    outcome = mon._submit_release_pair(k_near, k_far,
                                       near_submit_ok=True,
                                       far_submit_ok=False)
    assert outcome == "MTS_ENTRY_RECONCILE"
    # quarantine intent recorded
    assert any(e.get("reason") == "PARTIAL_SUBMISSION_QUARANTINE"
               for e in mon.events)
    # NO successful far ORDER_SUBMITTED
    assert not any(e.get("event") == "ORDER_SUBMITTED"
                   and e.get("contract") == "TMFI6" for e in mon.events)
    # first leg lock retained
    assert mon._leg_lock_check(k_near) is True
    # durable reconcile intent + restart restoration
    assert mon._reconcile_intent_exists(k_near) is True
    mon2 = _monitor(tmp_path)
    assert mon2._reconcile_intent_exists(k_near) is True
    # ambiguous broker query retains the first-leg lock
    assert mon2._leg_lock_apply_broker_query(k_near, trades=[]) is False
    assert mon2._leg_lock_check(k_near) is True
    # no auto retry / cancel / compensating orders
    assert not any("RETRY" in str(e.get("event") or "") for e in mon.events)
    assert not any("CANCEL" in str(e.get("event") or "") for e in mon.events)

