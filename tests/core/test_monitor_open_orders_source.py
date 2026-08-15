"""Broker open-order capture must prefer the SDK no-argument stream."""

from types import SimpleNamespace


def test_post_startup_snapshot_prefers_current_noarg_trades():
    from strategies.futures.monitor import FuturesMonitor

    calls = []

    class Api:
        futopt_account = SimpleNamespace(account_id="acct")
        stock_account = None

        def list_positions(self, *args, **kwargs):
            return []

        def list_trades(self, *args, **kwargs):
            calls.append((args, kwargs))
            if args or kwargs:
                return [SimpleNamespace(
                    code="TMFI6",
                    status=SimpleNamespace(status="PendingSubmit"),
                    id="STALE-ORDER",
                )]
            return []

        def margin(self, *args, **kwargs):
            return SimpleNamespace(available_margin=100000.0)

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = Api()
    monitor._execution_context = SimpleNamespace(
        session_id="session-current",
        account_id_hash="account-hash",
    )

    snapshot = monitor._capture_post_startup_snapshot()

    assert snapshot["fetch_status"]["capture"] == "OK"
    assert snapshot["open_orders"] == []
    assert calls and calls[0] == ((), {})

