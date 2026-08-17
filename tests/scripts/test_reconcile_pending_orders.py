"""RED/GREEN tests for scripts/reconcile_pending_orders.py (v2).

Broker-evidence contract (codex review):
- cancelled_at alone is NOT broker evidence (local order.cancel() stamps
  it too).  Reconciliation requires an explicit broker probe.
- broker None / probe failure -> fail-closed, NO changes.
- broker still lists the order open     -> retained (in-flight).
- broker shows a position on the symbol -> retained (local cancel wrong,
  order filled).
- broker shows neither                  -> marked cancelled.
- pending without cancelled_at          -> always retained.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from reconcile_pending_orders import reconcile, find_orders_file, BrokerProbe  # noqa: E402

BASE = {
    "order_id": "ORD-X",
    "symbol": "TMFI6",
    "side": "sell",
    "status": "pending_submit",
    "cancelled_at": None,
    "created_at": "2026-08-14T23:22:49",
    "broker_order_id": "7e2d5bc7",
    "quantity": 1,
    "price": 0,
}


class FakeBroker:
    def __init__(self, open_order_ids=(), positions=()):
        self._open = set(open_order_ids)
        self._pos = set(positions)

    def has_open_order(self, broker_order_id):
        return broker_order_id in self._open

    def has_position(self, symbol):
        return symbol in self._pos


def _write(tmp_path, orders):
    p = tmp_path / "TMF_20260817_orders.json"
    p.write_text(json.dumps(orders), encoding="utf-8")
    return str(p)


def _stale(oid="ORD-1", bid="7e2d5bc7", sym="TMFI6"):
    return {**BASE, "order_id": oid, "broker_order_id": bid, "symbol": sym,
            "status": "pending_submit",
            "cancelled_at": "2026-08-15T05:01:18"}


def test_broker_absent_and_no_position_marks_cancelled(tmp_path):
    p = _write(tmp_path, [_stale(), {**BASE, "order_id": "ORD-2", "status": "filled"}])
    res = reconcile(p, broker=FakeBroker())
    assert res["cancelled"] == ["ORD-1"]
    assert res["retained"] == []
    d = json.loads(open(p, encoding="utf-8").read())
    assert d[0]["status"] == "cancelled"
    assert d[1]["status"] == "filled"


def test_no_broker_probe_fail_closed_no_changes(tmp_path):
    p = _write(tmp_path, [_stale()])
    res = reconcile(p, broker=None)
    assert res["cancelled"] == []
    assert res["retained"] == ["ORD-1"]
    d = json.loads(open(p, encoding="utf-8").read())
    assert d[0]["status"] == "pending_submit"


def test_broker_still_lists_order_open_retained(tmp_path):
    p = _write(tmp_path, [_stale()])
    res = reconcile(p, broker=FakeBroker(open_order_ids={"7e2d5bc7"}))
    assert res["cancelled"] == []
    assert res["retained"] == ["ORD-1"]


def test_broker_has_position_local_cancel_was_wrong_retained(tmp_path):
    """The known historical failure: local watchdog cancel, broker FILLED."""
    p = _write(tmp_path, [_stale()])
    res = reconcile(p, broker=FakeBroker(positions={"TMFI6"}))
    assert res["cancelled"] == []
    assert res["retained"] == ["ORD-1"]
    d = json.loads(open(p, encoding="utf-8").read())
    assert d[0]["status"] == "pending_submit"


def test_pending_without_cancelled_at_always_retained(tmp_path):
    p = _write(tmp_path, [
        {**BASE, "order_id": "ORD-1", "status": "pending_submit",
         "cancelled_at": None},
    ])
    res = reconcile(p, broker=FakeBroker())
    assert res["cancelled"] == []
    assert res["retained"] == ["ORD-1"]


def test_dry_run_does_not_write(tmp_path):
    p = _write(tmp_path, [_stale()])
    before = open(p, encoding="utf-8").read()
    res = reconcile(p, broker=FakeBroker(), dry_run=True)
    assert res["cancelled"] == ["ORD-1"]
    assert open(p, encoding="utf-8").read() == before


def test_write_is_atomic_with_backup(tmp_path):
    p = _write(tmp_path, [_stale()])
    reconcile(p, broker=FakeBroker())
    assert os.path.exists(p + ".bak")
    json.loads(open(p, encoding="utf-8").read())


def test_find_orders_file_picks_latest(tmp_path):
    (tmp_path / "exports" / "trades").mkdir(parents=True)
    a = tmp_path / "exports" / "trades" / "TMF_20260810_orders.json"
    b = tmp_path / "exports" / "trades" / "TMF_20260817_orders.json"
    a.write_text("[]")
    b.write_text("[]")
    assert find_orders_file(str(tmp_path)) == str(b)


def test_broker_probe_queries_fail_closed_on_exception():
    class Boom:
        def list_trades(self):
            raise RuntimeError("query failed")

        def list_positions(self, **kwargs):
            raise RuntimeError("query failed")

    probe = BrokerProbe(Boom())
    assert probe.has_open_order("x") is True
    assert probe.has_position("TMFI6") is True
