"""RED tests: RECONCILED_EXIT_ONLY attestation flow (P0 completion).

Operator attestation is a HARD requirement: broker-automatic evidence
(position-detail: TMFH6 dseq=va042 only, no far-month receipt) cannot
uniquely attribute a two-leg spread.  Without a valid attestation + fresh
matching live_broker snapshot the system stays N/A + zero orders.
"""

import time
from types import SimpleNamespace

import pytest

from core.mode_transition import (
    ExecutionContext,
    LiveOrderBlocked,
    ModeTransitionState,
)
from core.order_management.order import OrderSide, OrderType
from core.order_management.order_manager import OrderManager


def _live_ctx():
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
        account_id_hash="a" * 64,
        session_id="b" * 32,
        config_hash="c" * 64,
    )


def _position_quarantine_ctx(*, reasons=(
        "POST_STARTUP_GATE_FAILED", "GUARD_POSITION_NOT_FLAT")):
    """A live session that is quarantined solely because it has positions.

    Normal certification must reject this state.  It may only obtain the
    narrower RECONCILED_EXIT_ONLY capability after the operator attests the
    exact broker snapshot; it must never become generally LIVE_READY here.
    """
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_QUARANTINED.value,
        live_order_allowed=False,
        account_id_hash="a" * 64,
        session_id="b" * 32,
        config_hash="c" * 64,
        audit_reasons=reasons,
    )


def _fresh_snapshot(positions=None, open_orders=None, now_ms=None):
    return {
        "source": "live_broker",
        "captured_at": now_ms if now_ms is not None else int(time.time() * 1000),
        "account_id_hash": "a" * 64,
        "session_id": "b" * 32,
        "config_hash": "c" * 64,
        "release_sha": "d" * 40,
        "positions": positions if positions is not None else [
            {"code": "TMFH6", "direction": "Sell", "quantity": 1,
             "avg_cost": 44909.0},
            {"code": "TMFI6", "direction": "Buy", "quantity": 1,
             "avg_cost": 45052.0},
        ],
        "open_orders": open_orders or [],
    }


def _valid_attestation(legs=None):
    return {
        "operator": "trader",
        "attested_at": "2026-08-11T11:00:00+08:00",
        "trade_id": "mts-20260811-085503",
        "evidence": "remaining spread is the 08:55 manual pair",
        "expected_legs": legs if legs is not None else [
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
        ],
    }


# ── attestation validation ────────────────────────────────────────────────

def test_attestation_missing_fails_closed():
    from core.reconciled_exit import AttestationError, build_exit_only_capability
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(None, _fresh_snapshot(), ctx=_live_ctx())
    assert exc.value.code == "ATTESTATION_INVALID"


def test_attestation_requires_operator_trade_id_and_two_legs():
    from core.reconciled_exit import AttestationError, build_exit_only_capability
    att = _valid_attestation()
    att["operator"] = ""
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(att, _fresh_snapshot(), ctx=_live_ctx())
    assert exc.value.code == "ATTESTATION_INVALID"

    att = _valid_attestation()
    att["evidence"] = ""
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(att, _fresh_snapshot(), ctx=_live_ctx())
    assert exc.value.code == "ATTESTATION_INVALID"

    att = _valid_attestation()
    att["trade_id"] = ""
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(att, _fresh_snapshot(), ctx=_live_ctx())
    assert exc.value.code == "ATTESTATION_INVALID"

    att = _valid_attestation()
    att["expected_legs"] = [att["expected_legs"][0]]
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(att, _fresh_snapshot(), ctx=_live_ctx())
    assert exc.value.code == "ATTESTATION_INVALID"


def test_attestation_rejects_secret_material_in_evidence():
    from core.reconciled_exit import AttestationError, build_exit_only_capability
    att = _valid_attestation()
    att["evidence"] = "cert PASSWORD=hunter2"
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(att, _fresh_snapshot(), ctx=_live_ctx())
    assert exc.value.code == "ATTESTATION_SECRET_REJECTED"


# ── snapshot validation ───────────────────────────────────────────────────

def test_snapshot_must_be_fresh_live_broker():
    from core.reconciled_exit import AttestationError, build_exit_only_capability

    snap = _fresh_snapshot()
    snap["source"] = "paper"
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(_valid_attestation(), snap, ctx=_live_ctx())
    assert exc.value.code == "SNAPSHOT_SOURCE_INVALID"

    snap = _fresh_snapshot(now_ms=int(time.time() * 1000) - 120_000)
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(_valid_attestation(), snap, ctx=_live_ctx())
    assert exc.value.code == "SNAPSHOT_STALE"


def test_open_orders_reject_attestation():
    from core.reconciled_exit import AttestationError, build_exit_only_capability
    snap = _fresh_snapshot(open_orders=[{"ordno": "X1"}])
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(_valid_attestation(), snap, ctx=_live_ctx())
    assert exc.value.code == "OPEN_ORDERS_NOT_EMPTY"


def test_snapshot_leg_mismatch_rejects_attestation():
    from core.reconciled_exit import AttestationError, build_exit_only_capability

    # wrong side
    snap = _fresh_snapshot(positions=[
        {"code": "TMFH6", "direction": "Buy", "quantity": 1,
         "avg_cost": 44909.0},
        {"code": "TMFI6", "direction": "Buy", "quantity": 1,
         "avg_cost": 45052.0},
    ])
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(_valid_attestation(), snap, ctx=_live_ctx())
    assert exc.value.code == "SNAPSHOT_LEG_MISMATCH"

    # extra position
    snap = _fresh_snapshot(positions=[
        {"code": "TMFH6", "direction": "Sell", "quantity": 1,
         "avg_cost": 44909.0},
        {"code": "TMFI6", "direction": "Buy", "quantity": 1,
         "avg_cost": 45052.0},
        {"code": "TMFI6", "direction": "Buy", "quantity": 1,
         "avg_cost": 44935.0},
    ])
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(_valid_attestation(), snap, ctx=_live_ctx())
    assert exc.value.code == "SNAPSHOT_LEG_MISMATCH"

    # wrong qty
    snap = _fresh_snapshot(positions=[
        {"code": "TMFH6", "direction": "Sell", "quantity": 2,
         "avg_cost": 44909.0},
        {"code": "TMFI6", "direction": "Buy", "quantity": 1,
         "avg_cost": 45052.0},
    ])
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(_valid_attestation(), snap, ctx=_live_ctx())
    assert exc.value.code == "SNAPSHOT_LEG_MISMATCH"


# ── success path ──────────────────────────────────────────────────────────

def test_attestation_success_builds_capability_and_record():
    from core.reconciled_exit import build_exit_only_capability

    capability, record = build_exit_only_capability(
        _valid_attestation(), _fresh_snapshot(), ctx=_live_ctx())

    assert capability["trade_id"] == "mts-20260811-085503"
    assert capability["reconciliation_id"] != capability["trade_id"]
    assert capability["account_id_hash"] == "a" * 64
    assert capability["session_id"] == "b" * 32
    assert capability["config_hash"] == "c" * 64
    assert capability["release_sha"] == "d" * 40
    assert capability["snapshot_hash"] == record["snapshot_hash"]
    # allowed_orders carry the CLOSING sides (cover short / close long)
    assert capability["allowed_orders"] == [
        {"symbol": "TMFH6", "side": "buy", "remaining_qty": 1},
        {"symbol": "TMFI6", "side": "sell", "remaining_qty": 1},
    ]

    assert record["operator"] == "trader"
    assert record["trade_id"] == "mts-20260811-085503"
    assert record["source"] == "live_broker"
    assert record["snapshot_hash"]
    assert record["legs"] == [
        {"symbol": "TMFH6", "side": "sell", "quantity": 1,
         "avg_cost": 44909.0},
        {"symbol": "TMFI6", "side": "buy", "quantity": 1,
         "avg_cost": 45052.0},
    ]
    # no secret fields in the dashboard record
    joined = str(record).upper()
    for banned in ("PASSWORD", "API_KEY", "PRIVATE KEY"):
        assert banned not in joined


def test_attestation_allows_only_position_quarantine_not_general_quarantine():
    from core.reconciled_exit import AttestationError, build_exit_only_capability

    # This is the real recovery path: normal certification rejects the
    # non-flat broker account, but the bounded exit capability may be issued.
    cap, _ = build_exit_only_capability(
        _valid_attestation(), _fresh_snapshot(), ctx=_position_quarantine_ctx())
    assert cap["reconciliation_id"]

    # A quarantine caused by a different safety failure cannot be converted
    # into an exit capability just by supplying an attestation.
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(
            _valid_attestation(), _fresh_snapshot(),
            ctx=_position_quarantine_ctx(reasons=("RELEASE_IDENTITY_MISMATCH",)))
    assert exc.value.code == "EXIT_ONLY_CONTEXT_INVALID"


def test_apply_exit_only_sets_distinct_mode_and_persists_capability():
    from core.reconciled_exit import (
        apply_exit_only,
        build_exit_only_capability,
    )

    capability, _ = build_exit_only_capability(
        _valid_attestation(), _fresh_snapshot(), ctx=_live_ctx())
    ctx = apply_exit_only(_live_ctx(), capability)

    assert ctx.effective_mode == ModeTransitionState.RECONCILED_EXIT_ONLY.value
    assert ctx.effective_mode != ModeTransitionState.LIVE_READY.value
    assert ctx.live_order_allowed is False
    assert ctx.exit_only_capability["reconciliation_id"] == capability["reconciliation_id"]

    data = ctx.to_dict()
    assert data["effective_mode"] == "reconciled_exit_only"
    assert data["exit_only_capability"]["reconciliation_id"] == \
        capability["reconciliation_id"]

    # original context unchanged
    assert _live_ctx().effective_mode == ModeTransitionState.LIVE_READY.value


# ── capability without attestation: N/A + zero orders ─────────────────────

def test_exit_only_without_attestation_is_zero_order():
    from core.order_management.order import Order
    from core.reconciled_exit import (
        apply_exit_only,
        build_exit_only_capability,
    )

    capability, _ = build_exit_only_capability(
        _valid_attestation(), _fresh_snapshot(), ctx=_live_ctx())
    ctx = apply_exit_only(_live_ctx(), capability)

    # an exit order WITHOUT the capability stamp is rejected (no stamp:
    # the order did not originate from create_order in exit-only mode)
    order = Order(
        symbol="TMFH6", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_EXIT")
    with pytest.raises(LiveOrderBlocked) as exc:
        ctx.assert_order_allowed(order, method="place_order")
    assert "RECONCILIATION_ID" in str(exc.value.reason)

    # a wrongly-stamped exit order is rejected too
    wrong = Order(
        symbol="TMFH6", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_EXIT", reconciliation_id="other-trade")
    with pytest.raises(LiveOrderBlocked) as exc:
        ctx.assert_order_allowed(wrong, method="place_order")
    assert "RECONCILIATION_ID" in str(exc.value.reason)

    # a stamped order with a NON-exit strategy is rejected (strategy gate:
    # check order is rid -> strategy -> scope, so the entry must be stamped
    # first to reach the strategy branch)
    stamped_entry = Order(
        symbol="TMFH6", side=OrderSide.SELL, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_ENTRY",
        reconciliation_id=capability["reconciliation_id"])
    with pytest.raises(LiveOrderBlocked) as exc:
        ctx.assert_order_allowed(stamped_entry, method="place_order")
    assert "STRATEGY_BLOCKED" in str(exc.value.reason)


# ── exit-order stamping via create_order ──────────────────────────────────

def test_exit_order_stamped_with_reconciliation_id_in_exit_only():
    from core.reconciled_exit import (
        apply_exit_only,
        build_exit_only_capability,
    )

    capability, _ = build_exit_only_capability(
        _valid_attestation(), _fresh_snapshot(), ctx=_live_ctx())
    ctx = apply_exit_only(_live_ctx(), capability)
    manager = OrderManager(mode="live", execution_context=ctx)

    exit_order = manager.create_order(
        symbol="TMFH6", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_EXIT")
    assert exit_order.reconciliation_id == capability["reconciliation_id"]

    release_order = manager.create_order(
        symbol="TMFI6", side=OrderSide.SELL, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_RELEASE")
    assert release_order.reconciliation_id == capability["reconciliation_id"]

    # OCO requires cancel/update cleanup, which exit-only intentionally
    # prohibits.  It cannot even create lifecycle state.
    with pytest.raises(LiveOrderBlocked) as exc:
        manager.create_order(
            symbol="TMFH6", side=OrderSide.BUY, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_RELEASE_OCO")
    assert exc.value.reason == "EXIT_ONLY_STRATEGY_BLOCKED"

    # One capability may issue each exact closing leg only once; no retry or
    # reissue is silently converted into another live order.
    with pytest.raises(LiveOrderBlocked) as exc:
        manager.create_order(
            symbol="TMFH6", side=OrderSide.BUY, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_EXIT")
    assert exc.value.reason == "EXIT_ONLY_ORDER_ALREADY_ISSUED"

    # Entry-class orders cannot even create local lifecycle state.
    with pytest.raises(LiveOrderBlocked) as exc:
        manager.create_order(
            symbol="TMFH6", side=OrderSide.SELL, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_ENTRY")
    assert exc.value.reason == "EXIT_ONLY_STRATEGY_BLOCKED"

    # the stamped exit order now passes the capability gate
    ctx.assert_order_allowed(exit_order, method="place_order")


def test_exit_order_not_stamped_when_live_ready():
    manager = OrderManager(mode="live", execution_context=_live_ctx())
    order = manager.create_order(
        symbol="TMFH6", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_EXIT")
    assert order.reconciliation_id is None


def test_exit_only_rejection_is_terminal_and_preserves_typed_reason():
    from core.order_management.order import Order
    from core.reconciled_exit import apply_exit_only, build_exit_only_capability

    capability, _ = build_exit_only_capability(
        _valid_attestation(), _fresh_snapshot(), ctx=_live_ctx())
    manager = OrderManager(
        mode="live", execution_context=apply_exit_only(_live_ctx(), capability))
    order = Order(symbol="TMFH6", side=OrderSide.SELL,
                  order_type=OrderType.MKP, quantity=1,
                  strategy="MTS_ENTRY", reconciliation_id="wrong")
    manager.active_orders[order.order_id] = order
    assert manager.submit(order) is False
    assert order.reject_reason == "EXIT_ONLY_RECONCILIATION_ID_MISMATCH"
    assert order.order_id not in manager.active_orders
    assert order in manager.completed


# ── monitor flow ──────────────────────────────────────────────────────────

def test_monitor_attestation_syncs_order_manager_and_client_e2e(
        monkeypatch, tmp_path):
    """[P0] after a successful attestation persist, BOTH client and the
    REAL OrderManager are synced to the exact same new immutable
    RECONCILED_EXIT_ONLY context — an exact MTS_RELEASE closing order is
    stamped with the reconciliation_id and passes the OrderManager
    authorization layer.  Prior behavior (stale LIVE_QUARANTINED in the
    order manager) rejects locally."""
    from types import SimpleNamespace

    from strategies.futures.monitor import FuturesMonitor
    from core.order_management.order_manager import OrderManager

    monkeypatch.setenv("LRC_RELEASE_SHA", "d" * 40)
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = SimpleNamespace(
        futopt_account=SimpleNamespace(),
        list_positions=lambda acct: [
            SimpleNamespace(code="TMFH6",
                            direction=SimpleNamespace(name="Sell"),
                            quantity=1, price=44909.0),
            SimpleNamespace(code="TMFI6",
                            direction=SimpleNamespace(name="Buy"),
                            quantity=1, price=45052.0),
        ],
        list_trades=lambda acct: [],
    )
    old_ctx = _position_quarantine_ctx()
    monitor._execution_context = old_ctx
    monitor._bind_reconciled_exit_identity = lambda: True
    adapter_calls = []
    adapter = SimpleNamespace(
        place_order_object=lambda order: adapter_calls.append(order))
    monitor.client = SimpleNamespace(_execution_context=old_ctx)
    monitor.order_mgr = OrderManager(mode="live", broker_adapter=adapter,
                                     execution_context=old_ctx)
    events = []
    monitor._append_mts_event = lambda event_type, **kw: events.append(
        (event_type, kw))

    record, err = monitor._operator_attest_exit_only(
        operator="trader",
        trade_id="mts-20260811-085503",
        evidence="remaining spread is the 08:55 manual pair",
        expected_legs=[
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
        ],
        attested_at="2026-08-11T11:00:00+08:00",
    )
    assert err is None
    assert monitor._execution_context.effective_mode == \
        ModeTransitionState.RECONCILED_EXIT_ONLY.value
    # the EXACT same immutable object is synced to both consumers
    assert monitor.order_mgr.execution_context is monitor._execution_context
    assert monitor.client._execution_context is monitor._execution_context
    # canonical file persisted
    assert (tmp_path / "execution_context.json").exists()

    _rid = monitor._execution_context.exit_only_capability[
        "reconciliation_id"]
    release_order = monitor.order_mgr.create_order(
        symbol="TMFH6", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_RELEASE")
    assert release_order.reconciliation_id == _rid
    # reaches the OrderManager authorization layer (adapter stub only:
    # no submission happened)
    monitor.order_mgr.execution_context.assert_order_allowed(
        release_order, method="place_order")
    assert adapter_calls == []


def test_persist_execution_context_returns_bool(monkeypatch, tmp_path):
    """[P1] _persist_execution_context returns bool: True on a
    successful canonical persist, False on persist failure."""
    import core.execution_context_state as ecs
    from strategies.futures.monitor import FuturesMonitor

    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._execution_context = _live_ctx()
    assert monitor._persist_execution_context() is True
    assert (tmp_path / "execution_context.json").exists()

    monkeypatch.setattr(
        ecs, "persist_execution_context",
        lambda payload: (_ for _ in ()).throw(RuntimeError("disk full")))
    assert monitor._persist_execution_context() is False


def test_monitor_attestation_persist_failure_rolls_back_no_event(
        monkeypatch, tmp_path):
    """[P1] attestation persist failure: monitor._execution_context is
    restored to the exact prior immutable context, client/order_mgr
    retain the same prior references, typed
    EXECUTION_CONTEXT_PERSIST_FAILED, NO OPERATOR_ATTESTATION success
    event, zero adapter calls."""
    from types import SimpleNamespace

    import core.execution_context_state as ecs
    from strategies.futures.monitor import FuturesMonitor
    from core.order_management.order_manager import OrderManager

    monkeypatch.setenv("LRC_RELEASE_SHA", "d" * 40)
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        ecs, "persist_execution_context",
        lambda payload: (_ for _ in ()).throw(RuntimeError("disk full")))

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = SimpleNamespace(
        futopt_account=SimpleNamespace(),
        list_positions=lambda acct: [
            SimpleNamespace(code="TMFH6",
                            direction=SimpleNamespace(name="Sell"),
                            quantity=1, price=44909.0),
            SimpleNamespace(code="TMFI6",
                            direction=SimpleNamespace(name="Buy"),
                            quantity=1, price=45052.0),
        ],
        list_trades=lambda acct: [],
    )
    old_ctx = _position_quarantine_ctx()
    monitor._execution_context = old_ctx
    monitor._bind_reconciled_exit_identity = lambda: True
    adapter_calls = []
    adapter = SimpleNamespace(
        place_order_object=lambda order: adapter_calls.append(order))
    monitor.client = SimpleNamespace(_execution_context=old_ctx)
    monitor.order_mgr = OrderManager(mode="live", broker_adapter=adapter,
                                     execution_context=old_ctx)
    events = []
    monitor._append_mts_event = lambda event_type, **kw: events.append(
        (event_type, kw))

    record, err = monitor._operator_attest_exit_only(
        operator="trader",
        trade_id="mts-20260811-085503",
        evidence="remaining spread is the 08:55 manual pair",
        expected_legs=[
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
        ],
        attested_at="2026-08-11T11:00:00+08:00",
    )
    assert record is None
    assert err == "EXECUTION_CONTEXT_PERSIST_FAILED"
    # exact prior immutable context restored on the monitor
    assert monitor._execution_context is old_ctx
    # consumers retain the same prior references
    assert monitor.client._execution_context is old_ctx
    assert monitor.order_mgr.execution_context is old_ctx
    # NO success event, zero adapter calls
    assert events == []
    assert adapter_calls == []


def test_monitor_attestation_persist_failure_keeps_prior_context(
        monkeypatch, tmp_path):
    """[P0] a persist failure leaves client + OrderManager on the PRIOR
    quarantined context (fail-closed) — zero adapter calls, no new allow
    state exposed; the stale context still rejects the exit order."""
    from types import SimpleNamespace

    import core.execution_context_state as ecs
    from strategies.futures.monitor import FuturesMonitor
    from core.order_management.order_manager import OrderManager

    monkeypatch.setenv("LRC_RELEASE_SHA", "d" * 40)
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        ecs, "persist_execution_context",
        lambda payload: (_ for _ in ()).throw(RuntimeError("disk full")))

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = SimpleNamespace(
        futopt_account=SimpleNamespace(),
        list_positions=lambda acct: [
            SimpleNamespace(code="TMFH6",
                            direction=SimpleNamespace(name="Sell"),
                            quantity=1, price=44909.0),
            SimpleNamespace(code="TMFI6",
                            direction=SimpleNamespace(name="Buy"),
                            quantity=1, price=45052.0),
        ],
        list_trades=lambda acct: [],
    )
    old_ctx = _position_quarantine_ctx()
    monitor._execution_context = old_ctx
    monitor._bind_reconciled_exit_identity = lambda: True
    adapter_calls = []
    adapter = SimpleNamespace(
        place_order_object=lambda order: adapter_calls.append(order))
    monitor.client = SimpleNamespace(_execution_context=old_ctx)
    monitor.order_mgr = OrderManager(mode="live", broker_adapter=adapter,
                                     execution_context=old_ctx)
    monitor._append_mts_event = lambda *args, **kwargs: None

    record, err = monitor._operator_attest_exit_only(
        operator="trader",
        trade_id="mts-20260811-085503",
        evidence="remaining spread is the 08:55 manual pair",
        expected_legs=[
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
        ],
        attested_at="2026-08-11T11:00:00+08:00",
    )
    # [P1] typed failure + full rollback: no success event, monitor
    # restored to the exact prior immutable context
    assert record is None
    assert err == "EXECUTION_CONTEXT_PERSIST_FAILED"
    assert monitor._execution_context is old_ctx
    # fail-closed: both consumers keep the PRIOR quarantined context
    assert monitor.client._execution_context is old_ctx
    assert monitor.order_mgr.execution_context is old_ctx
    assert adapter_calls == []
    # the stale quarantined context still rejects the exit order locally
    stale_order = monitor.order_mgr.create_order(
        symbol="TMFH6", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_RELEASE")
    assert stale_order.reconciliation_id is None
    with pytest.raises(LiveOrderBlocked):
        monitor.order_mgr.execution_context.assert_order_allowed(
            stale_order, method="place_order")


def test_monitor_attestation_flow_applies_exit_only_and_emits_event(monkeypatch):
    from types import SimpleNamespace

    from strategies.futures.monitor import FuturesMonitor

    monkeypatch.setenv("LRC_RELEASE_SHA", "d" * 40)

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = SimpleNamespace(
        futopt_account=SimpleNamespace(),
        list_positions=lambda acct: [
            SimpleNamespace(code="TMFH6",
                            direction=SimpleNamespace(name="Sell"),
                            quantity=1, price=44909.0),
            SimpleNamespace(code="TMFI6",
                            direction=SimpleNamespace(name="Buy"),
                            quantity=1, price=45052.0),
        ],
        list_trades=lambda acct: [],
    )
    monitor._execution_context = _live_ctx()
    events = []
    monitor._append_mts_event = lambda event_type, **kw: events.append(
        (event_type, kw))
    monitor._persist_execution_context = lambda: None

    record, err = monitor._operator_attest_exit_only(
        operator="trader",
        trade_id="mts-20260811-085503",
        evidence="remaining spread is the 08:55 manual pair",
        expected_legs=[
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
        ],
        attested_at="2026-08-11T11:00:00+08:00",
    )

    assert err is None
    assert record["trade_id"] == "mts-20260811-085503"
    assert monitor._execution_context.effective_mode == \
        ModeTransitionState.RECONCILED_EXIT_ONLY.value
    assert monitor._execution_context.live_order_allowed is False
    assert events and events[0][0] == "OPERATOR_ATTESTATION"
    assert events[0][1]["operator"] == "trader"


def test_monitor_attestation_rejected_without_broker(monkeypatch):
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = None
    monitor._execution_context = _live_ctx()
    events = []
    monitor._append_mts_event = lambda event_type, **kw: events.append(
        (event_type, kw))
    monitor._persist_execution_context = lambda: None

    record, err = monitor._operator_attest_exit_only(
        operator="trader",
        trade_id="mts-20260811-085503",
        evidence="no broker available",
        expected_legs=[
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
        ],
        attested_at="2026-08-11T11:00:00+08:00",
    )

    assert record is None
    # api=None cannot prove flat/open-orders evidence (fail-closed, N/A)
    assert err == "BROKER_SNAPSHOT_UNAVAILABLE"
    assert monitor._execution_context.effective_mode == \
        ModeTransitionState.LIVE_READY.value
    assert not events


def test_monitor_attestation_hydrates_bound_identity_in_position_quarantine(monkeypatch):
    """A non-flat startup cannot be LIVE_READY, yet must support only
    attested exit recovery after binding its current account/session/config.
    """
    import hashlib
    import core.live_route_certificate as certificate
    from strategies.futures.monitor import FuturesMonitor

    monkeypatch.setenv("LRC_RELEASE_SHA", "d" * 40)
    api = SimpleNamespace(
        futopt_account=SimpleNamespace(
            person_id="person", broker_id="broker", account_id="account"),
        list_positions=lambda acct: [
            SimpleNamespace(code="TMFH6", direction=SimpleNamespace(name="Sell"),
                            quantity=1, price=44909.0),
            SimpleNamespace(code="TMFI6", direction=SimpleNamespace(name="Buy"),
                            quantity=1, price=45052.0),
        ],
        list_trades=lambda acct: [],
    )
    monkeypatch.setattr(certificate.session_registry, "generation",
                        lambda candidate: "b" * 32)
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.api = api
    monitor.config_path = "/definitely-not-read-in-this-unit-test"
    monitor._execution_context = ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_QUARANTINED.value,
        live_order_allowed=False,
        audit_reasons=("POST_STARTUP_GATE_FAILED", "GUARD_POSITION_NOT_FLAT"),
    )
    monitor._append_mts_event = lambda *args, **kwargs: None
    monitor._persist_execution_context = lambda: None
    monitor._bind_reconciled_exit_identity = lambda: (
        setattr(monitor, "_execution_context", _position_quarantine_ctx(
            reasons=("POST_STARTUP_GATE_FAILED", "GUARD_POSITION_NOT_FLAT"))) or True)

    record, err = monitor._operator_attest_exit_only(
        operator="trader", trade_id="mts-20260811-085503",
        evidence="operator matched current broker positions",
        expected_legs=[
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
        ], attested_at="2026-08-11T11:00:00+08:00")

    assert err is None
    assert record["trade_id"] == "mts-20260811-085503"
    assert monitor._execution_context.effective_mode == \
        ModeTransitionState.RECONCILED_EXIT_ONLY.value


def test_capability_rejects_identity_mismatch_future_snapshot_and_bad_position():
    from core.reconciled_exit import AttestationError, build_exit_only_capability

    snap = _fresh_snapshot()
    snap["session_id"] = "e" * 32
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(_valid_attestation(), snap, ctx=_live_ctx())
    assert exc.value.code == "SNAPSHOT_IDENTITY_MISMATCH"

    snap = _fresh_snapshot(now_ms=int(time.time() * 1000) + 5_000)
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(_valid_attestation(), snap, ctx=_live_ctx())
    assert exc.value.code == "SNAPSHOT_STALE"

    snap = _fresh_snapshot(positions=[
        {"code": "TMFH6", "direction": "Sell", "quantity": "one",
         "avg_cost": 44909.0},
        {"code": "TMFI6", "direction": "Buy", "quantity": 1,
         "avg_cost": 45052.0},
    ])
    with pytest.raises(AttestationError) as exc:
        build_exit_only_capability(_valid_attestation(), snap, ctx=_live_ctx())
    assert exc.value.code == "SNAPSHOT_LEG_MISMATCH"


def test_exit_only_cannot_be_promoted_by_certificate_without_fresh_reconciliation():
    from core.reconciled_exit import apply_exit_only, build_exit_only_capability
    from core.live_route_certificate import RuntimeCertificationContext, transition_with_certificate

    capability, _ = build_exit_only_capability(
        _valid_attestation(), _fresh_snapshot(), ctx=_live_ctx())
    ctx = apply_exit_only(_live_ctx(), capability)
    runtime = RuntimeCertificationContext(
        process_start_id="test", account_hash="a" * 64,
        near_code="TMFH6", far_code="TMFI6", margin_source=None,
        session_generation="b" * 32, now_ts="2026-08-11T11:00:00+00:00")
    result = transition_with_certificate(ctx, object(), object(), runtime=runtime)
    assert result.effective_mode == ModeTransitionState.LIVE_QUARANTINED.value
    assert "EXIT_ONLY_RECONCILIATION_REQUIRED" in result.audit_reasons


# ── one-shot operator command ingress ─────────────────────────────────────

def test_exit_only_attestation_command_is_atomic_and_never_places_orders(
        monkeypatch, tmp_path):
    """The command can only request the stricter attestation transition.

    The monitor atomically consumes it; this test substitutes the attestation
    method so no broker and no order manager are involved.
    """
    import json
    import strategies.futures.monitor as monitor_module
    from strategies.futures.monitor import FuturesMonitor

    command_path = tmp_path / "commands" / "reconciled_exit_attestation.json"
    command_path.parent.mkdir()
    command_path.write_text(json.dumps({
        "command_id": "attest-001",
        "action": "ATTEST_EXIT_ONLY",
        "created_at": int(time.time() * 1000),
        "operator": "trader",
        "trade_id": "mts-20260811-085503",
        "evidence": "operator matched current broker positions",
        "expected_legs": [
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1},
        ],
    }), encoding="utf-8")
    monkeypatch.setattr(monitor_module, "runtime_path",
                        lambda *parts: str(command_path))

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    calls, events = [], []
    monitor._operator_attest_exit_only = lambda **kwargs: (
        calls.append(kwargs) or ({"snapshot_hash": "x"}, None))
    monitor._append_mts_event = lambda event_type, **kwargs: events.append(
        (event_type, kwargs))

    assert monitor._process_reconciled_exit_attestation_command() is True
    assert calls and calls[0]["trade_id"] == "mts-20260811-085503"
    assert not command_path.exists()
    assert not command_path.with_suffix(".json.processing").exists()
    assert monitor._exit_only_attestation_status == "ATTESTED"
    assert events[-1][0] == "OPERATOR_ATTESTATION_COMMAND_APPLIED"


def test_exit_only_attestation_command_rejects_stale_without_attestation(
        monkeypatch, tmp_path):
    import json
    import strategies.futures.monitor as monitor_module
    from strategies.futures.monitor import FuturesMonitor

    command_path = tmp_path / "reconciled_exit_attestation.json"
    command_path.write_text(json.dumps({
        "command_id": "attest-stale",
        "action": "ATTEST_EXIT_ONLY",
        "created_at": int(time.time() * 1000) - 61_000,
        "operator": "trader", "trade_id": "trade", "evidence": "e",
        "expected_legs": [],
    }), encoding="utf-8")
    monkeypatch.setattr(monitor_module, "runtime_path",
                        lambda *parts: str(command_path))
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    calls, events = [], []
    monitor._operator_attest_exit_only = lambda **kwargs: calls.append(kwargs)
    monitor._append_mts_event = lambda event_type, **kwargs: events.append(
        (event_type, kwargs))

    assert monitor._process_reconciled_exit_attestation_command() is True
    assert not calls
    assert monitor._exit_only_attestation_status == "REJECTED: COMMAND_EXPIRED"
    assert events[-1][0] == "OPERATOR_ATTESTATION_COMMAND_REJECTED"
    assert events[-1][1]["reason"] == "COMMAND_EXPIRED"
