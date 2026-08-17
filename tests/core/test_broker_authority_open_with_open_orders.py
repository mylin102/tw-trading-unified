"""RED: open_orders must NOT block the OPEN authority when both spread
legs are present in the canonical broker snapshot.

Runtime evidence (codex): canonical has TMFH6 Sell 1 + TMFI6 Buy 1 +
two PendingSubmit open_orders; the exit evaluator logs
RELEASE_EVAL_SKIP_NO_LOCAL_POSITION (local qty 0) because
_refresh_live_broker_authority() bails on open_orders before hydrating
the strategy legs.  open_orders must block NEW ENTRIES only; the OPEN
authority (near/far qty + side) must still be built for release/trail
evaluation.
"""

from types import SimpleNamespace


def _monitor(tmp_path):
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = True
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor._execution_context = SimpleNamespace(
        requested_mode="live", effective_mode="live_ready", session_id="sess")
    monitor._live_broker_authority_at = 0.0
    monitor._broker_position_observed = False
    monitor._live_broker_flat_proven = False
    monitor._broker_authority_degraded = False
    monitor._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "OK"},
        "account_identity_hash": "hash-1",
        "positions": [
            {"account": "futures", "code": "TMFH6", "quantity": 1,
             "direction": "Action.Sell", "avg_cost": 45879.0},
            {"account": "futures", "code": "TMFI6", "quantity": 1,
             "direction": "Action.Buy", "avg_cost": 46033.0},
        ],
        "open_orders": [
            {"order_id": "552bbf24", "code": "TMFH6",
             "status": "PendingSubmit"},
            {"order_id": "a90ba550", "code": "TMFI6",
             "status": "PendingSubmit"},
        ],
    }
    return monitor


def test_open_orders_with_both_legs_still_builds_open_authority(
        tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    from strategies.futures.mts_ledger_authority import MtsAuthority

    monitor = _monitor(tmp_path)
    strategy = SimpleNamespace(_has_position=False, _trade_id="old")
    auth = monitor._refresh_live_broker_authority(strategy)

    assert auth is not None
    assert auth.status is MtsAuthority.OPEN
    assert auth.near_side == "SHORT" and auth.far_side == "LONG"
    assert auth.near_qty == -1 and auth.far_qty == 1
    # the strategy must be hydrated for the exit evaluator
    assert strategy._has_position is True
    assert strategy._near_qty == 1 and strategy._far_qty == 1
    assert strategy._near_side == "SHORT" and strategy._far_side == "LONG"
    # open orders still keep entry blocked: position observed
    assert monitor._broker_position_observed is True
