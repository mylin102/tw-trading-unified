"""RED tests: exit-only snapshot capture TypeError fallback.

The preflight reference (core.live_broker_preflight._safe_open_orders) tries
``list_trades()`` first and falls back to ``list_trades(account)`` on
TypeError — the installed SDK is signature-sensitive.  The monitor capture
must behave the same or every attestation command is rejected with
BROKER_SNAPSHOT_UNAVAILABLE.
"""

from types import SimpleNamespace

from core.mode_transition import (
    ExecutionContext,
    ModeTransitionState,
)


def _live_ctx():
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
    )


def _make_api(*, trades_with_acct, trades_noarg,
              positions_with_acct, positions_noarg):
    def _trades(acct=None):
        if acct is not None:
            if isinstance(trades_with_acct, Exception):
                raise trades_with_acct
            return trades_with_acct
        return trades_noarg

    def _positions(acct=None):
        if acct is not None:
            if isinstance(positions_with_acct, Exception):
                raise positions_with_acct
            return positions_with_acct
        return positions_noarg

    return SimpleNamespace(
        futopt_account=SimpleNamespace(),
        list_positions=_positions,
        list_trades=_trades,
    )


def test_capture_falls_back_to_noarg_list_trades_on_typeerror():
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = _make_api(
        trades_with_acct=TypeError("list_trades() takes no account"),
        trades_noarg=[SimpleNamespace(
            status=SimpleNamespace(status="Filled"))],
        positions_with_acct=[],
        positions_noarg=[],
    )
    monitor._execution_context = _live_ctx()

    snap = monitor._capture_exit_only_snapshot()

    assert snap["source"] == "live_broker"
    assert snap.get("capture_error") is not True
    # the Filled row came from the no-arg fallback and is filtered terminal
    assert snap["open_orders"] == []


def test_capture_falls_back_to_noarg_list_positions_on_typeerror():
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = _make_api(
        trades_with_acct=[],
        trades_noarg=[],
        positions_with_acct=TypeError("list_positions() takes no account"),
        positions_noarg=[SimpleNamespace(
            code="TMFH6", direction=SimpleNamespace(name="Sell"),
            quantity=1, price=44909.0)],
    )
    monitor._execution_context = _live_ctx()

    snap = monitor._capture_exit_only_snapshot()

    assert snap["source"] == "live_broker"
    assert snap.get("capture_error") is not True
    assert snap["positions"] == [{
        "code": "TMFH6", "direction": "Sell",
        "quantity": 1, "avg_cost": 44909.0,
    }]
