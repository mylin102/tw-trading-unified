"""Snapshot projection contract: build_session_snapshot MUST expose BOTH
projections — "trades" (all normalized rows incl. terminal, for lifecycle
reconciliation) and "open_orders" (non-terminal only, for watchdog
consumers).  Regression for the covered-matching slice accident where a
stale local copy overwrite dropped the "trades" key.
"""
from types import SimpleNamespace

import pytest

from core.broker_evidence import build_session_snapshot


def _trade(**kw):
    base = dict(id="B-1", ordno="B-1", seqno="S-1", code="TMFH6",
                quantity=1, status=SimpleNamespace(status="PendingSubmit"))
    base.update(kw)
    return SimpleNamespace(**base)


def test_snapshot_exposes_trades_and_open_orders_projections():
    snap = build_session_snapshot(
        session_id="sess-proj",
        positions=[],
        trades=[
            _trade(id="B-FILLED", ordno="O-F", seqno="S-F",
                   status=SimpleNamespace(status="Filled")),
            _trade(id="B-PENDING", ordno="O-P", seqno="S-P",
                   status=SimpleNamespace(status="PendingSubmit")),
        ],
        captured_at=3,
    )
    # both projection keys must exist (terminal evidence retained)
    assert "trades" in snap
    assert "open_orders" in snap
    assert {r["status"] for r in snap["trades"]} == {"Filled", "PendingSubmit"}
    assert [r["status"] for r in snap["open_orders"]] == ["PendingSubmit"]


def test_snapshot_trades_include_terminal_and_open_orders_never_do():
    snap = build_session_snapshot(
        session_id="sess-proj-2",
        positions=[],
        trades=[
            _trade(id="B-CANCEL", ordno="O-C", seqno="S-C",
                   status=SimpleNamespace(status="Cancelled")),
            _trade(id="B-PEND2", ordno="O-P2", seqno="S-P2",
                   status=SimpleNamespace(status="PendingSubmit")),
        ],
        captured_at=3,
    )
    assert len(snap["trades"]) == 2
    assert len(snap["open_orders"]) == 1
    assert all(not r["status"] in ("Filled", "Cancelled", "Rejected", "Expired")
               for r in snap["open_orders"])
