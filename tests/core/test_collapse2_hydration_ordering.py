"""Collapse 2 RED contract: atomic exit hydration/authorization ordering."""

import json

import pytest


def _position():
    return {
        "mode": "reconciled_exit_only",
        "live_order_allowed": True,
        "legs": [
            {"code": "TMFH6", "side": "sell", "quantity": 1},
            {"code": "TMFI6", "side": "buy", "quantity": 1},
        ],
    }


def _coordinator(**kwargs):
    from core.exit_only_position import ReconciledPositionHydration
    return ReconciledPositionHydration(**kwargs)


def test_m1_evaluate_before_hydrate_has_no_intent_or_submit_and_reverse_can_send():
    calls = []
    c = _coordinator()
    blocked = c.evaluate_exit("i-1", "key-1")
    assert blocked.status == "HYDRATION_REQUIRED"
    assert blocked.intent is None
    assert c.submit("i-1", "key-1", lambda: calls.append(1)) is False
    assert calls == []

    c.reconcile(_position())
    allowed = c.evaluate_exit("i-1", "key-1")
    assert allowed.status == "AUTHORIZED"
    assert c.submit("i-1", "key-1", lambda: (calls.append(1) or True)) is True
    assert calls == [1]


def test_m2_ordering_rejection_retries_once_same_identity_then_terminal():
    calls = []
    c = _coordinator()
    c.reconcile(_position())
    assert c.evaluate_exit("i-2", "key-2").status == "AUTHORIZED"
    c.record_rejection("i-2", reason="HYDRATION_AUTHORIZATION_ORDERING", receipt=None)
    assert c.retry_once("i-2", "key-2", lambda: (calls.append(1) or True),
                        active_orders=[], position=_position()) is True
    assert calls == [1]
    c.record_rejection("i-2", reason="HYDRATION_AUTHORIZATION_ORDERING", receipt=None)
    assert c.retry_once("i-2", "key-2", lambda: calls.append(1),
                        active_orders=[], position=_position()) is False
    assert calls == [1]


def test_m3_active_order_blocks_retry():
    c = _coordinator()
    c.reconcile(_position())
    c.evaluate_exit("i-3", "key-3")
    c.record_rejection("i-3", reason="HYDRATION_AUTHORIZATION_ORDERING", receipt=None)
    assert c.retry_once("i-3", "key-3", lambda: pytest.fail("must not submit"),
                        active_orders=[{"id": "other"}], position=_position()) is False


def test_m4_position_mismatch_blocks_retry():
    c = _coordinator()
    c.reconcile(_position())
    c.evaluate_exit("i-4", "key-4")
    c.record_rejection("i-4", reason="HYDRATION_AUTHORIZATION_ORDERING", receipt=None)
    changed = {**_position(), "legs": [{"code": "TMFH6", "side": "buy", "quantity": 1},
                                        _position()["legs"][1]]}
    assert c.retry_once("i-4", "key-4", lambda: pytest.fail("must not submit"),
                        active_orders=[], position=changed) is False


def test_m5_duplicate_idempotency_is_local_reject():
    calls = []
    c = _coordinator()
    c.reconcile(_position())
    c.evaluate_exit("i-5", "same-key")
    assert c.submit("i-5", "same-key", lambda: (calls.append(1) or True)) is True
    assert c.submit("i-5b", "same-key", lambda: (calls.append(1) or True)) is False
    assert calls == [1]


def test_m6_partial_combo_recovers_only_filled_legs():
    c = _coordinator()
    recovery = c.partial_fill_recovery(
        [{"code": "TMFH6", "quantity": 1}, {"code": "TMFI6", "quantity": 0}])
    assert recovery == [{"code": "TMFH6", "quantity": 1}]


def test_m7_restart_restores_durable_intent_without_resend_until_reconcile():
    calls = []
    c = _coordinator()
    c.reconcile(_position())
    c.evaluate_exit("i-7", "key-7")
    c.record_rejection("i-7", reason="BROKER_TIMEOUT", receipt=None)
    restored = _coordinator(durable_intents=json.loads(json.dumps(c.durable_view())))
    assert restored.submit("i-7", "key-7", lambda: calls.append(1)) is False
    assert calls == []
    assert restored.intent_state("i-7") == "PENDING_RECONCILE"


def test_m8_timeout_or_unknown_stays_pending_and_never_retries():
    c = _coordinator()
    c.reconcile(_position())
    c.evaluate_exit("i-8", "key-8")
    for reason in ("BROKER_TIMEOUT", "UNKNOWN"):
        c.record_rejection("i-8", reason=reason, receipt=None)
        assert c.retry_once("i-8", "key-8", lambda: pytest.fail("must not submit"),
                            active_orders=[], position=_position()) is False
        assert c.intent_state("i-8") == "PENDING_RECONCILE"


def test_m9_guards_have_behavioral_effects():
    c = _coordinator()
    assert c.evaluate_exit("i-9", "key-9").status == "HYDRATION_REQUIRED"
    c.reconcile(_position())
    c.evaluate_exit("i-9", "key-9")
    c.record_rejection("i-9", reason="HYDRATION_AUTHORIZATION_ORDERING")
    assert c.retry_once("i-9", "key-9", lambda: True,
                        active_orders=[{"id": "active"}],
                        position=_position()) is False
    assert c.authorization_ready(_position()) is True
    assert c.authorization_ready({**_position(), "legs": []}) is False


def test_gateway_scoped_exit_keeps_identity_route_session_gates():
    from core.order_intent_gateway import OrderIntentGateway

    gw = OrderIntentGateway()
    base = {
        "live": True, "mode": "reconciled_exit_only",
        "reconciled_position_hydrated": True, "exit_authorized": True,
        "position_has_position": True,
    }
    for field, reason in (("identity_ok", "GATEWAY_IDENTITY_FAILED"),
                          ("route_ok", "GATEWAY_ROUTE_FAILED"),
                          ("session_ok", "GATEWAY_SESSION_FAILED")):
        denied = dict(base, **{field: False})
        assert gw.authorize_intent(action="EXIT", strategy="MTS_EXIT",
                                   authority=denied)[2] == reason
    assert gw.authorize_intent(
        action="EXIT", strategy="MTS_EXIT",
        authority=dict(base, identity_ok=True, route_ok=True,
                       session_ok=True))[0] is True
