"""Reconciliation rule 2026-08-19: broker-confirmed leg-lock retirement.

Rule 2: broker snapshot succeeded -> broker positions/orders are the only
truth; local extra locks are marked RETIRED_UNRESOLVED (terminal) with a
manifest record.
Rule 3: UNCERTAIN/failed broker query -> never clear, never guess.
Rule 4: local stale state must not block confirmed-absent broker state.

Covers _reconcile_leg_locks_from_snapshot + _leg_lock_check terminal set.
"""
import json

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
    import os

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

    import os

    manifest = os.path.join(
        os.path.dirname(mon._leg_lock_path()),
        "mts_leg_locks_reconcile_manifest.jsonl")
    assert os.path.exists(manifest)
    with open(manifest, encoding="utf-8") as f:
        rec = json.loads(f.readline().strip())
    assert rec["broker_order_id"] == "broker-absent"
    assert rec["contract"] == "TMFI6"


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


# ── Rule 3: no broker identity -> never guess ──────────────────────────


def test_lock_without_broker_id_never_retired(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 0
    locks = mon._leg_lock_load()
    assert locks[mon._leg_lock_id(key)]["status"] == "PENDING_UNCONFIRMED"


# ── Rule 3: UNCERTAIN / non-live snapshot -> zero mutation ─────────────


@pytest.mark.parametrize("snapshot", [
    _good_snapshot(source="paper_strategy"),
    _good_snapshot(fetch_status={"capture": "FAILED"}),
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


# ── Idempotency: already-terminal lock skipped ─────────────────────────


def test_already_terminal_lock_skipped(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent",
              status="RETIRED_UNRESOLVED")

    n = mon._reconcile_leg_locks_from_snapshot(_good_snapshot())

    assert n == 0


# ── Rule 4: _leg_lock_check must NOT block retired locks ───────────────


def test_leg_lock_check_passes_retired_lock(tmp_path):
    mon = _monitor(tmp_path)
    key = _lock_key()
    _add_lock(mon, key, broker_order_id="broker-absent",
              status="RETIRED_UNRESOLVED")

    # A second signal for this leg must NOT be blocked by the retired lock
    # and must NOT emit ORDER_BLOCKED_PENDING_EXISTS.
    assert mon._leg_lock_check(key) is False
    assert not any(e.get("reason") == "ORDER_BLOCKED_PENDING_EXISTS"
                   for e in mon.events)
