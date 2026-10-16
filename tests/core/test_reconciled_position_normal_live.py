"""RED contract tests for the single reconciled-position lifecycle.

These tests deliberately describe the post-Task-A architecture: a position
that reconciles to broker truth is ordinary LIVE_READY state.  There is no
exit-only capability or quarantine intermediary.
"""

from types import SimpleNamespace

from core.mode_transition import ModeTransitionState
from core.order_intent_gateway import OrderIntentGateway
from core.reconciliation.holding_state import (
    ReconciliationStatus,
    reconcile_holding,
)


def _leg(code, side, qty=1, avg_cost=100.0):
    return {"code": code, "side": side, "qty": qty, "avg_cost": avg_cost}


def test_reconciled_startup_is_normal_live_and_hydrates_before_entries():
    decision = reconcile_holding(
        broker_legs=[_leg("TMFI6", "LONG", avg_cost=45876.0),
                     _leg("TMFJ6", "SHORT", avg_cost=46059.0)],
        local_legs=[_leg("TMFI6", "LONG", avg_cost=45876.0),
                    _leg("TMFJ6", "SHORT", avg_cost=46059.0)],
        tick_size=1.0,
    )

    assert decision.status is ReconciliationStatus.RECONCILED
    assert decision.context_mode == ModeTransitionState.LIVE_READY.value
    assert decision.hydrated is True
    assert decision.exit_only_capability is None
    assert decision.quarantine is False
    assert decision.entry_blocked is True  # existing open position


def test_standard_exit_uses_base_gateway_authorization_without_capability():
    gateway = OrderIntentGateway(process_epoch="test")
    ok, binding, reason = gateway.authorize_intent(
        action="EXIT",
        strategy="MTS_EXIT",
        authority={
            "live": True,
            "mode": ModeTransitionState.LIVE_READY.value,
            "live_order_allowed": True,
            "position_has_position": True,
            "identity_ok": True,
            "route_ok": True,
            "session_ok": True,
        },
    )
    assert ok is True
    assert binding is None
    assert reason is None


def test_mismatch_converges_to_broker_and_preserves_raw_values():
    decision = reconcile_holding(
        broker_legs=[_leg("TMFI6", "LONG", avg_cost=45876.0),
                     _leg("TMFJ6", "SHORT", avg_cost=46059.0)],
        local_legs=[_leg("TMFI6", "LONG", avg_cost=45870.0),
                    _leg("TMFJ6", "SHORT", avg_cost=46059.0)],
        tick_size=1.0,
    )

    assert decision.status is ReconciliationStatus.RECONCILED
    assert decision.context_mode == ModeTransitionState.LIVE_READY.value
    assert decision.fail_closed is False
    assert decision.raw_broker_legs[0]["avg_cost"] == 45876.0
    assert decision.raw_local_legs[0]["avg_cost"] == 45870.0


def test_uncertain_broker_query_is_reconciliation_hold():
    decision = reconcile_holding(
        broker_legs=None,
        local_legs=[_leg("TMFI6", "LONG"), _leg("TMFJ6", "SHORT")],
        tick_size=1.0,
        broker_query_error="timeout",
    )
    assert decision.status is ReconciliationStatus.HOLD
    assert decision.fail_closed is True
