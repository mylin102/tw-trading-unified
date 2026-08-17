"""RED: restart reconciliation consumes one canonical broker snapshot."""
import json

from scripts.reconcile_pending_orders import reconcile


class SnapshotBroker:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.calls = 0

    def capture_snapshot(self, *, session_id):
        self.calls += 1
        assert session_id
        return self.snapshot

    def has_open_order(self, _broker_id):
        raise AssertionError("legacy per-order query must not run")

    def has_position(self, _symbol):
        raise AssertionError("legacy per-order query must not run")


def test_reconcile_uses_one_snapshot_for_all_pending_orders(tmp_path):
    path = tmp_path / "orders.json"
    path.write_text(json.dumps([
        {"order_id": "ORD-1", "symbol": "TMFI6", "status": "pending_submit",
         "broker_order_id": "B-1"},
        {"order_id": "ORD-2", "symbol": "TMFH6", "status": "pending_submit",
         "broker_order_id": "B-2"},
    ]), encoding="utf-8")
    broker = SnapshotBroker({
        "source": "live_broker", "session_id": "sess", "captured_at": 1,
        "positions": [], "open_orders": [], "trades": [],
    })
    result = reconcile(str(path), broker=broker)
    assert broker.calls == 1
    assert result["cancelled"] == ["ORD-1", "ORD-2"]
