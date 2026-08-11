"""RED tests: EXIT_ONLY position hydration + decision binding.

Hydrate an attested broker pair into a managed exit-only position so the
existing Policy J / combined / single-release evaluation produces ONLY
capability-bound closing orders.  No synthetic PnL; missing/stale/
ambiguous BBO -> N/A + zero order; paper unchanged.
"""

import time

import pytest

from core.mode_transition import (
    ExecutionContext,
    LiveOrderBlocked,
    ModeTransitionState,
)
from core.order_management.order import OrderSide, OrderType
from core.order_management.order_manager import OrderManager


def _capability():
    return {
        "schema_version": 2,
        "reconciliation_id": "recon-abc123",
        "trade_id": "mts-20260811-085503",
        "snapshot_hash": "s" * 64,
        "attestation_hash": "a" * 64,
        "snapshot_captured_at": 1754991000000,
        "account_id_hash": "b" * 64,
        "session_id": "c" * 32,
        "config_hash": "d" * 64,
        "release_sha": "e" * 40,
        "allowed_orders": [
            {"symbol": "TMFH6", "side": "buy", "remaining_qty": 1},
            {"symbol": "TMFI6", "side": "sell", "remaining_qty": 1},
        ],
        "legs": [
            {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1,
             "avg_cost": 44909.0},
            {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1,
             "avg_cost": 45052.0},
        ],
    }


_MISSING = object()


def _exit_only_ctx(cap=_MISSING):
    if cap is _MISSING:
        cap = _capability()
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.RECONCILED_EXIT_ONLY.value,
        live_order_allowed=False,
        exit_only_capability=cap,
    )


def _live_ctx():
    return ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
    )


def _bbo_slots(now=None):
    ts = now if now is not None else time.time()
    return {
        "TMF": {"bid": 44900.0, "ask": 44910.0, "bidask_at": ts},
        "TMF_FAR": {"bid": 45040.0, "ask": 45060.0, "bidask_at": ts},
    }


def _pure_bbo_slots(now=None):
    slots = _bbo_slots(now)
    return {"near": slots["TMF"], "far": slots["TMF_FAR"]}


# ── pure hydration ────────────────────────────────────────────────────────

def test_hydrate_requires_valid_capability():
    from core.exit_only_position import hydrate_exit_only_position
    from core.reconciled_exit import AttestationError

    with pytest.raises(AttestationError) as exc:
        hydrate_exit_only_position(None)
    assert exc.value.code == "EXIT_ONLY_CAPABILITY_MISSING"

    bad = _capability()
    bad.pop("legs")
    with pytest.raises(AttestationError) as exc:
        hydrate_exit_only_position(bad)
    assert exc.value.code == "EXIT_ONLY_CAPABILITY_INVALID"

    bad = _capability()
    bad["legs"][0]["avg_cost"] = 0.0
    with pytest.raises(AttestationError) as exc:
        hydrate_exit_only_position(bad)
    assert exc.value.code == "EXIT_ONLY_CAPABILITY_INVALID"


def test_hydrate_preserves_broker_costs_and_trade_id_no_pnl():
    from core.exit_only_position import hydrate_exit_only_position

    position = hydrate_exit_only_position(_capability())

    assert position["trade_id"] == "mts-20260811-085503"
    assert position["reconciliation_id"] == "recon-abc123"
    assert position["snapshot_hash"] == "s" * 64
    assert position["has_position"] is True
    assert position["mode"] == "reconciled_exit_only"
    assert position["legs"] == [
        {"code": "TMFH6", "side": "sell", "quantity": 1, "avg_cost": 44909.0},
        {"code": "TMFI6", "side": "buy", "quantity": 1, "avg_cost": 45052.0},
    ]
    # no synthetic PnL anywhere
    joined = str(position).upper()
    assert "PNL" not in joined and "UPL" not in joined


# ── BBO binding ───────────────────────────────────────────────────────────

def test_bbo_binding_missing_stale_ambiguous():
    from core.exit_only_position import build_bbo_binding

    assert build_bbo_binding(None)[0] is None
    assert build_bbo_binding({"near": {}, "far": {}})[1] == "BBO_MISSING"
    assert build_bbo_binding({
        "near": {"bid": 1, "ask": 2, "bidask_at": time.time()},
        "far": {},
    })[1] == "BBO_MISSING"
    assert build_bbo_binding(_pure_bbo_slots(now=time.time() - 60))[1] \
        == "BBO_STALE"
    ambiguous = _pure_bbo_slots()
    ambiguous["near"]["bid"] = 44920.0  # bid > ask
    assert build_bbo_binding(ambiguous)[1] == "BBO_AMBIGUOUS"


def test_bbo_binding_valid():
    from core.exit_only_position import build_bbo_binding

    binding, reason = build_bbo_binding(_pure_bbo_slots())
    assert reason is None
    assert binding["bbo_hash"]
    assert binding["bbo_captured_at"] > 0


def test_attach_decision_binding():
    from core.exit_only_position import attach_decision_binding, build_bbo_binding

    binding, _ = build_bbo_binding(_pure_bbo_slots())
    event = {"order_id": "ORD-1", "trade_id": "mts-20260811-085503"}
    bound = attach_decision_binding(event, _capability(), binding)

    assert bound["reconciliation_id"] == "recon-abc123"
    assert bound["position_snapshot_hash"] == "s" * 64
    assert bound["bbo_hash"] == binding["bbo_hash"]
    assert bound["bbo_captured_at"] == binding["bbo_captured_at"]
    assert bound["order_id"] == "ORD-1"
    assert "bbo_hash" not in event  # original unchanged


# ── monitor hydration ─────────────────────────────────────────────────────

def _monitor(ctx, slots=None):
    from strategies.futures.monitor import FuturesMonitor

    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor._execution_context = ctx
    monitor.market_data = dict(slots or {})
    monitor.ticker = "TMF"
    monitor._exit_only_position = None
    monitor._exit_only_decision_binding = None
    events = []
    monitor._append_mts_event = lambda t, **k: events.append((t, k))
    monitor._persist_execution_context = lambda: None
    return monitor, events


def _bound_strategy(rid="recon-abc123", trade_id="mts-20260811-085503"):
    from types import SimpleNamespace
    return SimpleNamespace(
        _trade_id=trade_id, _reconciliation_id=rid,
        _near_side="SHORT", _far_side="LONG",
        _near_qty=1, _far_qty=1, _has_position=True)


def test_monitor_hydrates_position_and_emits_event():
    monitor, events = _monitor(_exit_only_ctx())

    monitor._hydrate_exit_only_position()

    assert monitor._exit_only_position is not None
    assert monitor._exit_only_position["legs"][0]["avg_cost"] == 44909.0
    assert events and events[0][0] == "EXIT_ONLY_POSITION_HYDRATED"
    assert events[0][1]["trade_id"] == "mts-20260811-085503"


def test_monitor_hydration_fails_without_capability():
    monitor, events = _monitor(_exit_only_ctx(cap=None))

    monitor._hydrate_exit_only_position()

    assert monitor._exit_only_position is None
    assert events and events[0][0] == "EXIT_ONLY_HYDRATION_FAILED"


def test_paper_unchanged_hydration_noop():
    monitor, events = _monitor(_live_ctx())

    monitor._hydrate_exit_only_position()

    assert monitor._exit_only_position is None
    assert not events


# ── decision guard ────────────────────────────────────────────────────────

def test_exit_only_guard_blocks_entry():
    monitor, events = _monitor(_exit_only_ctx())

    ok, binding, reason = monitor._exit_only_decision_guard("BUY_NEAR_SELL_FAR")

    assert ok is False and binding is None
    assert reason == "EXIT_ONLY_ENTRY_BLOCKED"


def test_exit_only_guard_missing_bbo_zero_order():
    monitor, events = _monitor(_exit_only_ctx())
    monitor._hydrate_exit_only_position()
    events.clear()

    ok, binding, reason = monitor._exit_only_decision_guard(
        "COMBINED_EXIT", _bound_strategy())

    assert ok is False and binding is None
    assert reason == "BBO_MISSING"
    assert not events  # no order attempt


def test_exit_only_guard_eligible_with_valid_bbo():
    monitor, events = _monitor(_exit_only_ctx(), _bbo_slots())
    monitor._hydrate_exit_only_position()
    events.clear()

    ok, binding, reason = monitor._exit_only_decision_guard(
        "COMBINED_EXIT", _bound_strategy())

    assert ok is True and reason is None
    assert binding["bbo_hash"]

    ok, binding, reason = monitor._exit_only_decision_guard(
        "RELEASE_NEAR", _bound_strategy())
    assert ok is True and reason is None


def test_paper_guard_passes_through_without_binding():
    monitor, _ = _monitor(_live_ctx())

    ok, binding, reason = monitor._exit_only_decision_guard("COMBINED_EXIT")

    assert ok is True and binding is None and reason is None


# ── order creation stays capability-bound ─────────────────────────────────

def test_exit_only_eligible_creates_only_capability_bound_orders():
    ctx = _exit_only_ctx()
    manager = OrderManager(mode="live", execution_context=ctx)

    near = manager.create_order(
        symbol="TMFH6", side=OrderSide.BUY, order_type=OrderType.MKP,
        quantity=1, strategy="MTS_EXIT")
    assert near.reconciliation_id == "recon-abc123"
    ctx.assert_order_allowed(near, method="place_order")

    with pytest.raises(LiveOrderBlocked) as exc:
        manager.create_order(
            symbol="TMFI6", side=OrderSide.BUY, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_EXIT")  # off-scope symbol
    assert "SCOPE_MISMATCH" in str(exc.value.reason)

    with pytest.raises(LiveOrderBlocked) as exc:
        manager.create_order(
            symbol="TMFH6", side=OrderSide.SELL, order_type=OrderType.MKP,
            quantity=1, strategy="MTS_ENTRY")  # entry remains blocked
    assert "STRATEGY_BLOCKED" in str(exc.value.reason)
