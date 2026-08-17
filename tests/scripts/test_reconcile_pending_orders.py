"""RED/GREEN tests for scripts/reconcile_pending_orders.py.

Locked behaviours:
- pending_submit + cancelled_at (broker terminal evidence) -> cancelled
- pending_submit WITHOUT cancelled_at -> retained (fail-closed)
- non-pending statuses (filled/... ) -> untouched
- dry-run reports but does not write
- write-back is atomic (backup + valid JSON after)
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from reconcile_pending_orders import reconcile, find_orders_file  # noqa: E402

BASE = {
    "order_id": "ORD-X",
    "symbol": "TMFI6",
    "side": "sell",
    "status": "pending_submit",
    "cancelled_at": None,
    "created_at": "2026-08-14T23:22:49",
    "broker_order_id": "abc",
    "quantity": 1,
    "price": 0,
}


def _write(tmp_path, orders):
    p = tmp_path / "TMF_20260817_orders.json"
    p.write_text(json.dumps(orders), encoding="utf-8")
    return str(p)


def test_stale_pending_with_cancelled_at_marked_cancelled(tmp_path):
    p = _write(tmp_path, [
        {**BASE, "order_id": "ORD-1", "status": "pending_submit",
         "cancelled_at": "2026-08-15T05:01:18"},
        {**BASE, "order_id": "ORD-2", "status": "filled"},
    ])
    changed = reconcile(p)
    assert changed == ["ORD-1"]
    d = json.loads(open(p, encoding="utf-8").read())
    assert d[0]["status"] == "cancelled"
    assert d[1]["status"] == "filled"


def test_pending_without_cancelled_at_retained(tmp_path):
    p = _write(tmp_path, [
        {**BASE, "order_id": "ORD-1", "status": "pending_submit",
         "cancelled_at": None},
    ])
    changed = reconcile(p)
    assert changed == []
    d = json.loads(open(p, encoding="utf-8").read())
    assert d[0]["status"] == "pending_submit"


def test_dry_run_does_not_write(tmp_path):
    p = _write(tmp_path, [
        {**BASE, "order_id": "ORD-1", "status": "pending_submit",
         "cancelled_at": "2026-08-15T05:01:18"},
    ])
    before = open(p, encoding="utf-8").read()
    changed = reconcile(p, dry_run=True)
    assert changed == ["ORD-1"]
    assert open(p, encoding="utf-8").read() == before


def test_write_is_atomic_with_backup(tmp_path):
    p = _write(tmp_path, [
        {**BASE, "order_id": "ORD-1", "status": "pending_submit",
         "cancelled_at": "2026-08-15T05:01:18"},
    ])
    reconcile(p)
    assert os.path.exists(p + ".bak")
    json.loads(open(p, encoding="utf-8").read())  # valid JSON after


def test_find_orders_file_picks_latest(tmp_path):
    (tmp_path / "exports" / "trades").mkdir(parents=True)
    a = tmp_path / "exports" / "trades" / "TMF_20260810_orders.json"
    b = tmp_path / "exports" / "trades" / "TMF_20260817_orders.json"
    a.write_text("[]")
    b.write_text("[]")
    assert find_orders_file(str(tmp_path)) == str(b)
