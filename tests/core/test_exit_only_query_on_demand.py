"""RED: exit-only authorization is query-on-demand.

Whenever exit-only authorization / renewal / pre-submit proof needs
positions / open_orders / BBO evidence it performs an IMMEDIATE read-only
broker query through the existing live session and uses the returned
current evidence directly.  The persisted canonical snapshot is a
display/audit cache ONLY — never the authorization authority.  Query
failure/mismatch stays fail-closed (quarantine).  Zero order calls.
"""
import json

from types import SimpleNamespace

_CAP = {
    "reconciliation_id": "rid-1",
    "account_id_hash": "acc-hash",
    "session_id": "sess-1",
    "config_hash": "cfg-hash",
    "release_sha": "",
    "legs": [
        {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
        {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
    ],
}

_POS = [
    SimpleNamespace(code="TMFH6", direction="sell", quantity=1,
                    price=46077.0),
    SimpleNamespace(code="TMFI6", direction="buy", quantity=1,
                    price=45231.0),
]


def _exit_ctx():
    return SimpleNamespace(
        effective_mode="reconciled_exit_only",
        exit_only_capability=_CAP,
        account_id_hash="acc-hash",
        session_id="sess-1",
        config_hash="cfg-hash",
    )


def _make_api(positions, trades):
    calls = []

    class _Api:
        futopt_account = object()

        def list_positions(self, acct=None):
            calls.append("list_positions")
            return positions

        def list_trades(self, acct=None):
            calls.append("list_trades")
            return trades

        def place_order(self, *a, **k):
            calls.append("place_order")

        def cancel_order(self, *a, **k):
            calls.append("cancel_order")

        def update_order(self, *a, **k):
            calls.append("update_order")

    return _Api(), calls


def _proof(monkeypatch):
    from strategies.futures.monitor import FuturesMonitor
    monkeypatch.setenv("LRC_RELEASE_SHA", "")
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api, calls = _make_api(_POS, [])
    monitor._execution_context = _exit_ctx()
    monitor._exit_only_renewal_fail = (
        lambda reason, quarantine=True: (False, reason))
    return monitor, calls


def test_pre_submit_proof_fresh_query_overrides_stale_persisted_artifact(
        tmp_path, monkeypatch):
    """A stale persisted canonical artifact (old timestamp) must NOT
    block the exit authorization when the immediate fresh read-only
    broker query succeeds."""
    monitor, _calls = _proof(monkeypatch)
    _stale = tmp_path / "broker_snapshot_canonical.json"
    _stale.write_text(json.dumps({
        "source": "live_broker", "captured_at": 1,
        "positions": [], "open_orders": [],
    }), encoding="utf-8")
    _ok, _reason = monitor._pre_submit_exit_only_proof()
    assert _ok is True
    assert _reason is None


def test_pre_submit_proof_query_failure_blocks(monkeypatch):
    """A fresh-query failure blocks the authorization fail-closed
    (typed quarantine reason), even when a persisted artifact exists."""
    from strategies.futures.monitor import FuturesMonitor
    monkeypatch.setenv("LRC_RELEASE_SHA", "")
    monitor = FuturesMonitor.__new__(FuturesMonitor)

    class _FailingApi:
        futopt_account = object()

        def list_positions(self, acct=None):
            raise RuntimeError("broker session down")

        def list_trades(self, acct=None):
            return []

    monitor.api = _FailingApi()
    monitor._execution_context = _exit_ctx()
    monitor._exit_only_renewal_fail = (
        lambda reason, quarantine=True: (False, reason))
    _ok, _reason = monitor._pre_submit_exit_only_proof()
    assert _ok is False
    assert _reason == "EXIT_ONLY_RENEWAL_QUERY_FAILED"


def test_pre_submit_proof_capture_is_read_only_no_order_cancel(monkeypatch):
    """The pre-submit evidence capture issues read-only broker calls
    (list_positions / list_trades) and ZERO order/cancel/update calls."""
    monitor, calls = _proof(monkeypatch)
    _ok, _reason = monitor._pre_submit_exit_only_proof()
    assert _ok is True
    assert "list_positions" in calls
    assert "list_trades" in calls
    assert "place_order" not in calls
    assert "cancel_order" not in calls
    assert "update_order" not in calls
