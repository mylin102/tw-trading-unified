"""Phase 2 RED: canonical snapshots retain terminal broker evidence."""
from types import SimpleNamespace

from core.broker_evidence import build_session_snapshot


def test_snapshot_retains_all_normalized_trades_for_terminal_reconciliation():
    filled = SimpleNamespace(
        id="B-FILLED", ordno="O-FILLED", seqno="S-FILLED",
        code="TMFI6", quantity=1,
        status=SimpleNamespace(status="Filled"),
    )
    pending = SimpleNamespace(
        id="B-PENDING", ordno="O-PENDING", seqno="S-PENDING",
        code="TMFH6", quantity=1,
        status=SimpleNamespace(status="PendingSubmit"),
    )
    snap = build_session_snapshot(
        session_id="sess-2", positions=[], trades=[filled, pending], captured_at=2,
    )
    assert {row["status"] for row in snap["trades"]} == {"Filled", "PendingSubmit"}
    assert [row["status"] for row in snap["open_orders"]] == ["PendingSubmit"]
