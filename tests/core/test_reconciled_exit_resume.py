"""RED contract: RECONCILED_EXIT_ONLY completes only after broker-flat proof."""

import time
from types import SimpleNamespace

import pytest

from core.mode_transition import ExecutionContext, ModeTransitionState


def _ctx():
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
        account_id_hash="a" * 64,
        session_id="b" * 32,
        config_hash="c" * 64,
    )


def _attestation():
    return {
        "operator": "trader",
        "attested_at": "2026-08-11T12:00:00+08:00",
        "trade_id": "mts-20260811-085503",
        "evidence": "operator confirmed remaining manual spread",
        "expected_legs": [
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
        ],
    }


def _snapshot(*, flat=False, session="b" * 32, age_ms=0, open_orders=None):
    return {
        "source": "live_broker",
        "captured_at": int(time.time() * 1000) - age_ms,
        "account_id_hash": "a" * 64,
        "session_id": session,
        "config_hash": "c" * 64,
        "release_sha": "d" * 40,
        "positions": [] if flat else [
            {"code": "TMFH6", "direction": "sell", "quantity": 1, "avg_cost": 44909.0},
            {"code": "TMFI6", "direction": "buy", "quantity": 1, "avg_cost": 45052.0},
        ],
        "open_orders": [] if open_orders is None else open_orders,
    }


def _exit_ctx():
    from core.reconciled_exit import apply_exit_only, build_exit_only_capability
    cap, _ = build_exit_only_capability(_attestation(), _snapshot(), ctx=_ctx())
    return apply_exit_only(_ctx(), cap)


def _orders(ctx, statuses=("filled", "filled")):
    from core.order_management.order import Order, OrderSide, OrderStatus, OrderType
    cap = ctx.exit_only_capability
    created = []
    for allowed, status in zip(cap["allowed_orders"], statuses):
        order = Order(
            symbol=allowed["symbol"],
            side=OrderSide.BUY if allowed["side"] == "buy" else OrderSide.SELL,
            order_type=OrderType.MKP,
            quantity=allowed["remaining_qty"],
            strategy="MTS_EXIT",
            reconciliation_id=cap["reconciliation_id"],
        )
        order.status = OrderStatus(status)
        created.append(order)
    return created


def test_only_two_exact_capability_orders_both_filled_complete():
    from core.reconciled_exit import capability_exit_completed
    ctx = _exit_ctx()
    assert capability_exit_completed(ctx, _orders(ctx)) is True
    assert capability_exit_completed(ctx, _orders(ctx, ("filled", "partial_filled"))) is False


def test_local_fills_alone_never_resume_or_revoke_capability():
    from core.reconciled_exit import capability_exit_completed
    ctx = _exit_ctx()
    assert capability_exit_completed(ctx, _orders(ctx)) is True
    # Completion detection only proves local terminal callbacks.  It cannot
    # change mode without a new live-broker flat snapshot.
    assert ctx.effective_mode == ModeTransitionState.RECONCILED_EXIT_ONLY.value
    assert ctx.exit_only_capability is not None


def test_flat_snapshot_revokes_capability_to_quarantine_not_live_ready():
    from core.reconciled_exit import revoke_exit_only_after_flat_snapshot
    ctx = _exit_ctx()
    next_ctx, record = revoke_exit_only_after_flat_snapshot(ctx, _snapshot(flat=True))
    assert next_ctx.effective_mode == ModeTransitionState.LIVE_QUARANTINED.value
    assert next_ctx.live_order_allowed is False
    assert next_ctx.exit_only_capability is None
    assert next_ctx.audit_reasons == ("EXIT_ONLY_FLAT_RECONCILED",)
    assert record["snapshot_hash"]


@pytest.mark.parametrize("bad", [
    lambda: _snapshot(flat=False),
    lambda: _snapshot(flat=True, open_orders=[{"ordno": "open"}]),
    lambda: _snapshot(flat=True, session="e" * 32),
    lambda: _snapshot(flat=True, age_ms=61_000),
    lambda: {"source": "unavailable", "capture_error": True},
])
def test_flat_revoke_requires_fresh_same_session_broker_proof(bad):
    from core.reconciled_exit import AttestationError, revoke_exit_only_after_flat_snapshot
    with pytest.raises(AttestationError):
        revoke_exit_only_after_flat_snapshot(_exit_ctx(), bad())


def _monitor_for_exit_only(statuses=("filled", "filled"), snapshot=None):
    from strategies.futures.monitor import FuturesMonitor
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._execution_context = _exit_ctx()
    monitor.order_mgr = SimpleNamespace(active_orders={}, completed=_orders(
        monitor._execution_context, statuses))
    monitor._capture_exit_only_snapshot = lambda: snapshot or _snapshot(flat=True)
    monitor._persist_execution_context = lambda: setattr(monitor, "persisted", True)
    monitor.events = []
    monitor._append_mts_event = lambda typ, **kw: monitor.events.append((typ, kw))
    monitor._clear_mts_entry_reconcile_intents = lambda *args: True
    return monitor


def test_monitor_reconciles_only_after_both_filled_and_broker_flat():
    monitor = _monitor_for_exit_only()
    record, error = monitor._maybe_finalize_exit_only_reconciliation()
    assert error is None
    assert record["snapshot_hash"]
    assert monitor.persisted is True
    assert monitor._execution_context.effective_mode == ModeTransitionState.LIVE_QUARANTINED.value
    assert monitor._execution_context.exit_only_capability is None
    assert monitor.events[-1][0] == "EXIT_ONLY_FLAT_RECONCILED"


def test_monitor_single_local_fill_never_resumes_or_queries_broker():
    monitor = _monitor_for_exit_only(statuses=("filled", "submitted"))
    called = []
    monitor._capture_exit_only_snapshot = lambda: called.append(True) or _snapshot(flat=True)
    record, error = monitor._maybe_finalize_exit_only_reconciliation()
    assert (record, error) == (None, None)
    assert not called
    assert monitor._execution_context.effective_mode == ModeTransitionState.RECONCILED_EXIT_ONLY.value


@pytest.mark.parametrize("status", ["partial_filled", "cancelled", "rejected", "expired"])
def test_monitor_exit_terminal_anomaly_quarantines_without_cancel(status):
    monitor = _monitor_for_exit_only(statuses=(status, "submitted"))
    cancel_calls = []
    monitor.order_mgr.cancel = lambda *args, **kwargs: cancel_calls.append((args, kwargs))
    record, error = monitor._maybe_finalize_exit_only_reconciliation()
    assert record is None
    assert error == "EXIT_ONLY_TERMINAL_AMBIGUITY"
    assert monitor._execution_context.effective_mode == ModeTransitionState.LIVE_QUARANTINED.value
    assert monitor._execution_context.exit_only_capability is not None
    assert not cancel_calls
