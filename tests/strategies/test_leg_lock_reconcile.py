"""Reconciliation rule 2026-08-19: broker-confirmed leg-lock retirement.

Rule 2: broker snapshot succeeded -> broker positions/orders are the only
truth; local extra locks are marked RETIRED_UNRESOLVED (terminal) with a
manifest record.
Rule 3: UNCERTAIN/failed broker query -> never clear, never guess.
Rule 4: local stale state must not block confirmed-absent broker state.

Covers _reconcile_leg_locks_from_snapshot + _leg_lock_check terminal set.
Audit 2026-08-20 additions:
  A. read-modify-write under ONE exclusive flock (no lost concurrent locks)
  B. empty contract identity -> fail-closed keep (never guess)
  C. manifest write failure -> no retirement, WARN event, no false success
"""
import contextlib
import json
import os
import threading
import time

import pytest

from strategies.futures.monitor import FuturesMonitor


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
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon._leg_lock_store = str(tmp_path / "leg_locks.json")
    mon._pending_lifecycle_orders = {}
    mon.events = []
    mon._append_mts_event = lambda *a, **k: mon.events.append(k)
    return mon


def _good_snapshot(**over):
    snap = {
        "source": "live_broker",
        "fetch_status": {"capture": "OK"},
        "open_orders": [],
        "positions": [],
        "broker_trades": [],
        "snapshot_generation": "gen-1",
        "captured_at": 1234567890,
    }
    snap.update(over)
    return snap


def _add_lock(mon, key, broker_order_id="broker-1", status="PENDING_UNCONFIRMED"):
    """Write a lock directly into the store (bypass acquire)."""
    locks = {}
    p = mon._leg_lock_path()
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            locks = json.load(f) or {}
    locks[mon._leg_lock_id(key)] = {
        "trade_id": key["trade_id"],
        "session_generation": key["session_generation"],
        "contract": key["contract"],
        "closing_side": key["closing_side"],
        "qty": key["qty"],
        "status": status,
        "broker_order_id": broker_order_id,
    }
    with open(p, "w", encoding="utf-8") as f:
        json.dump(locks, f, default=str)
    return p


def _manifest_path(mon):
    return os.path.join(os.path.dirname(mon._leg_lock_path()),
                        "mts_leg_locks_reconcile_manifest.jsonl")


# ── Rule 2: retire broker-confirmed-stale lock ──────────────────────────


def test_stale_lock_retired_when_broker_has_neither_order_nor_position(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 1
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(key)]["status"] == "RETIRED_UNRESOLVED"
    assert locks[mon._leg_lock_id(key)]["terminal"] == "BROKER_RECONCILED"
    assert any(e.get("reason") == "LEG_LOCK_RETIRED_BY_SNAPSHOT"
               for e in mon.events)


def test_retired_lock_writes_manifest(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent")

    mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    manifest = _manifest_path(mon)
    assert os.path.exists(manifest)
    with open(manifest, encoding="utf-8") as f:
        rec = json.loads(f.readline().strip())
    assert rec["broker_order_id"] == "broker-absent"
    assert rec["contract"] == "TMFI6"
    assert rec["stage"] == "prepared"


def test_successful_reconcile_appends_committed_record(tmp_path):
    """Two-phase manifest: after the lock write succeeds a committed record
    must follow the prepared record — the retirement is only true once
    committed exists."""
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 1
    manifest = _manifest_path(mon)
    with open(manifest, encoding="utf-8") as f:
        lines = [json.loads(l.strip()) for l in f if l.strip()]
    stages = [r["stage"] for r in lines]
    assert stages == ["prepared", "committed"]
    assert lines[1]["lock_id"] == mon._leg_lock_id(key)
    assert lines[1]["broker_order_id"] == "broker-absent"


# ── Rule 2 guard: broker still has the open order -> keep ──────────────


def test_lock_kept_when_broker_still_has_open_order(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-live")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot(
        open_orders=[{"id": "broker-live", "status": "Submitted"}]))

    assert n == 0
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(key)]["status"] == "PENDING_UNCONFIRMED"


# ── Rule 2 guard: broker still has a position on the contract -> keep ──


def test_lock_kept_when_broker_still_has_position(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key(contract="TMFH6")
    _add_lock(mon, key, broker_order_id="broker-x")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot(
        positions=[{"account": "futures", "code": "TMFH6", "quantity": 1}]))

    assert n == 0
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(key)]["status"] == "PENDING_UNCONFIRMED"


# ── Rule 2 guard: unrelated active lock is preserved alongside retirement ──


def test_reconcile_preserves_unrelated_active_lock(tmp_path):
    """A reconcile that retires one stale lock must not disturb a second,
    still-live lock in the same store (no whole-file clobber)."""
    mon = _monitor(tmp_path)
    stale_key = _lock_key(trade_id="mts-auto-111111-111")
    live_key = _lock_key(trade_id="mts-auto-222222-222")
    _add_lock(mon, stale_key, broker_order_id="broker-absent")
    _add_lock(mon, live_key, broker_order_id="broker-live")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot(
        open_orders=[{"id": "broker-live", "status": "Submitted"}]))

    assert n == 1
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(stale_key)]["status"] == "RETIRED_UNRESOLVED"
    assert locks[mon._leg_lock_id(live_key)]["status"] == "PENDING_UNCONFIRMED"


# ── Rule 3: no broker identity -> never guess ──────────────────────────


def test_lock_without_broker_id_never_retired(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 0
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(key)]["status"] == "PENDING_UNCONFIRMED"


@pytest.mark.parametrize("snapshot", [
    {"source": "paper"},
    {"source": "live_broker", "fetch_status": {"capture": "UNCERTAIN"}},
    None,
])
def test_non_authoritative_snapshot_never_mutates(tmp_path, snapshot):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent")

    n = mon._reconcile_leg_locks_from_snapshot(snapshot)

    assert n == 0
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(key)]["status"] == "PENDING_UNCONFIRMED"


def test_already_terminal_lock_skipped(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent",
              status="RETIRED_UNRESOLVED")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 0
    assert not any(e.get("reason") == "LEG_LOCK_RETIRED_BY_SNAPSHOT"
                   for e in mon.events)


# ── Rule 4: terminal set unblocks _leg_lock_check ───────────────────────


def test_leg_lock_check_passes_retired_lock(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent",
              status="RETIRED_UNRESOLVED")

    blocked = mon._leg_lock_check(key)

    # _leg_lock_check returns bool: False = leg is NOT blocked (retired lock
    # must not block a second signal).
    assert blocked is False
    assert not any(e.get("reason") == "ORDER_BLOCKED_PENDING_EXISTS"
                   for e in mon.events)


# ── Audit A: read-modify-write under ONE exclusive flock ────────────────


def test_reconcile_holds_single_exclusive_flock_across_read_and_write(
        tmp_path, monkeypatch):
    """Audit A (structure): read and write must share one exclusive flock.

    The old implementation called _leg_lock_load() (SHARED flock, released
    immediately) then _leg_lock_save() (EXCLUSIVE flock) much later — a
    concurrent writer could add an active lock in between and get clobbered
    by the stale dict.  Instrument the flock and assert that both the read
    and the write happen while an EXCLUSIVE flock is held.
    """
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent")

    state = {"in_exclusive": False, "read_inside": False, "write_inside": False}
    orig_flock = mon._leg_lock_flock
    orig_read = mon._leg_lock_read
    orig_write = mon._leg_lock_write

    @contextlib.contextmanager
    def tracked_flock(exclusive=True):
        prev = state["in_exclusive"]
        state["in_exclusive"] = exclusive
        try:
            with orig_flock(exclusive=exclusive) as f:
                yield f
        finally:
            state["in_exclusive"] = prev

    def tracked_read():
        if state["in_exclusive"]:
            state["read_inside"] = True
        return orig_read()

    def tracked_write(locks):
        if state["in_exclusive"]:
            state["write_inside"] = True
        return orig_write(locks)

    monkeypatch.setattr(mon, "_leg_lock_flock", tracked_flock)
    monkeypatch.setattr(mon, "_leg_lock_read", tracked_read)
    monkeypatch.setattr(mon, "_leg_lock_write", tracked_write)

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 1
    assert state["read_inside"], "read must happen inside the exclusive flock"
    assert state["write_inside"], "write must happen inside the exclusive flock"


def test_concurrent_lock_added_during_reconcile_is_preserved(tmp_path, monkeypatch):
    """Audit A (behavior): a lock added by a concurrent writer while
    reconcile runs must survive.  With the fix, the reconcile holds ONE
    exclusive flock for the whole read-modify-write, so the writer blocks
    until reconcile finishes and its lock lands afterwards.  Before the fix,
    the writer could slip into the load→save window and get clobbered by the
    stale dict write (this test then fails).
    """
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent")

    new_key = _lock_key(trade_id="mts-auto-999999-999")
    read_started = threading.Event()
    orig_read = mon._leg_lock_read

    def hooked_read():
        r = orig_read()
        read_started.set()
        return r

    monkeypatch.setattr(mon, "_leg_lock_read", hooked_read)

    result = {}

    def concurrent_writer():
        read_started.wait(timeout=5)
        time.sleep(0.05)  # give reconcile time to leave the read and enter work
        try:
            with mon._leg_lock_flock(exclusive=True):
                cur = mon._leg_lock_read()
                cur[mon._leg_lock_id(new_key)] = {
                    "trade_id": new_key["trade_id"],
                    "session_generation": new_key["session_generation"],
                    "contract": new_key["contract"],
                    "closing_side": new_key["closing_side"],
                    "qty": new_key["qty"],
                    "status": "PENDING_UNCONFIRMED",
                    "broker_order_id": "broker-concurrent-live",
                }
                mon._leg_lock_write(cur)
            result["ok"] = True
        except Exception as exc:  # pragma: no cover - failure surfaces via result
            result["ok"] = False
            result["err"] = repr(exc)

    t = threading.Thread(target=concurrent_writer)
    t.start()

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot(
        open_orders=[{"id": "broker-concurrent-live", "status": "Submitted"}]))

    t.join(timeout=5)

    assert result.get("ok") is True, result.get("err", "writer did not finish")
    assert n == 1
    locks = mon._leg_lock_load()
    # The stale lock was retired...
    assert locks[mon._leg_lock_id(key)]["status"] == "RETIRED_UNRESOLVED"
    # ...and the concurrently-added active lock is NOT lost.
    assert mon._leg_lock_id(new_key) in locks
    assert locks[mon._leg_lock_id(new_key)]["status"] == "PENDING_UNCONFIRMED"


# ── Audit B: empty contract identity -> fail-closed keep ────────────────


def test_lock_with_broker_id_but_empty_contract_never_retired(tmp_path):
    """Audit B: broker_order_id present but contract empty is an incomplete
    identity.  The old guard `if _ct and _ct in _pos_codes` silently failed
    for empty contract, letting the lock fall through to retirement.  An
    empty contract must be treated like a missing identity: never guess.
    """
    mon = _monitor(tmp_path)
    key = _lock_key(contract="")
    _add_lock(mon, key, broker_order_id="broker-absent")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot(
        positions=[{"account": "futures", "code": "TMFI6", "quantity": 1}]))

    assert n == 0
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(key)]["status"] == "PENDING_UNCONFIRMED"
    assert not any(e.get("reason") == "LEG_LOCK_RETIRED_BY_SNAPSHOT"
                   for e in mon.events)


# ── Audit C: manifest write failure -> no retirement, no false success ──


def test_manifest_write_failure_prevents_retirement(tmp_path):
    """Audit C: if the manifest cannot be written, the lock must NOT be
    retired and the reconcile must not report success.  The old code retired
    first, emitted the event, and swallowed the manifest exception with
    `pass`, producing a retirement with no audit evidence.
    """
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent")

    # Make the manifest path unwritable: create a DIRECTORY where the
    # manifest file would go, so open(..., "a") raises IsADirectoryError.
    os.makedirs(_manifest_path(mon), exist_ok=True)

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 0
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(key)]["status"] == "PENDING_UNCONFIRMED"
    assert not any(e.get("reason") == "LEG_LOCK_RETIRED_BY_SNAPSHOT"
                   for e in mon.events)
    assert any(e.get("reason") == "LEG_LOCK_RECONCILE_MANIFEST_FAILED"
               for e in mon.events)


def test_lock_write_failure_reported_as_failure(tmp_path, monkeypatch):
    """Audit C companion: if the lock write itself fails (raises), reconcile
    returns 0 and emits no RETIRED event — no false success.
    """
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent")

    def boom_write(locks):
        raise OSError("disk full")

    monkeypatch.setattr(mon, "_leg_lock_write", boom_write)

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 0
    assert not any(e.get("reason") == "LEG_LOCK_RETIRED_BY_SNAPSHOT"
                   for e in mon.events)


def test_lock_write_failure_appends_retracted_record(tmp_path, monkeypatch):
    """Review 2026-08-20: manifest and lock write are not atomic.  If the
    lock write fails after the prepared record, the manifest must NOT claim
    a retirement that did not happen: a retracted record must follow, and
    LEG_LOCK_RECONCILE_LOCK_WRITE_FAILED must fire.
    """
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent")

    def boom_write(locks):
        raise OSError("disk full")

    monkeypatch.setattr(mon, "_leg_lock_write", boom_write)

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 0
    # Lock is still alive on disk.
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(key)]["status"] == "PENDING_UNCONFIRMED"
    # Manifest: prepared followed by retracted — no committed, no phantom.
    manifest = _manifest_path(mon)
    with open(manifest, encoding="utf-8") as f:
        lines = [json.loads(l.strip()) for l in f if l.strip()]
    stages = [r["stage"] for r in lines]
    assert stages == ["prepared", "retracted"]
    assert lines[1]["reason"] == "lock_write_failed"
    assert lines[1]["lock_id"] == mon._leg_lock_id(key)
    # Failure event fired; no false success event.
    assert any(e.get("reason") == "LEG_LOCK_RECONCILE_LOCK_WRITE_FAILED"
               for e in mon.events)
    assert not any(e.get("reason") == "LEG_LOCK_RETIRED_BY_SNAPSHOT"
                   for e in mon.events)
