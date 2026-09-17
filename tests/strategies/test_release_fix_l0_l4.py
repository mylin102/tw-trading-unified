import json
from pathlib import Path

import pytest

from core.futures_release_guard import (
    PremiseError,
    ReleaseIntent,
    evaluate_release_precondition,
    sanitize_startup_state,
    validate_release_effect,
)


def _position(code="TMFJ6", side="SHORT", qty=1):
    return {"code": code, "side": side, "qty": qty, "avg_cost": 46753.0}


def _broker(positions, *, available=True):
    return {
        "source": "live_broker" if available else "unavailable",
        "positions": positions,
        "fetch_status": {"capture": "OK" if available else "FAIL"},
        "canonical_input_hash": "b" * 64,
    }


def _release(premise, *, symbol="TMFJ6", side="BUY"):
    return ReleaseIntent(
        action="RELEASE_NEAR", symbol=symbol, side=side, qty=1,
        premise=premise,
    )


def test_t_l1_missing_premise_is_rejected():
    with pytest.raises(PremiseError):
        ReleaseIntent(action="RELEASE_NEAR", symbol="TMFJ6",
                      side="BUY", qty=1, premise=None)


def test_r1_flat_broker_rejects_stale_short_release_and_audits():
    decision = evaluate_release_precondition(
        _release({"kind": "HOLD_LEG", "leg": "near", "side": "SHORT", "qty": 1}),
        _broker([]),
    )
    assert decision.allowed is False
    assert decision.reason == "BROKER_PREMISE_MISMATCH"
    assert decision.audit["event"] == "ORDER_INTENT_BLOCKED"
    assert decision.audit["broker_truth"] == "[]"


def test_r2_startup_flat_state_enters_no_order_mode_and_clears_decision_fields():
    state = {
        "has_position": True, "state": "POSITION",
        "near_side": "SHORT", "far_side": "LONG",
        "released_leg": "near", "remaining_leg": "far",
        "near_entry": 46288.0, "far_entry": 46656.0,
    }
    decision = sanitize_startup_state(
        state, _broker([]), near_symbol="TMFJ6", far_symbol="TMFK6"
    )
    assert decision.release_allowed is False
    assert decision.reason == "BROKER_FLAT_NO_RELEASE"
    assert decision.state["has_position"] is False
    for field in ("near_side", "far_side", "released_leg", "remaining_leg",
                  "near_entry", "far_entry"):
        assert decision.state[field] is None


def test_r3_broker_holds_near_short_release_is_allowed():
    decision = evaluate_release_precondition(
        _release({"kind": "HOLD_LEG", "leg": "near", "side": "SHORT", "qty": 1}),
        _broker([_position()]),
    )
    assert decision.allowed is True


def test_r4_unavailable_broker_is_fail_closed():
    decision = evaluate_release_precondition(
        _release({"kind": "HOLD_LEG", "leg": "near", "side": "SHORT", "qty": 1}),
        _broker([], available=False),
    )
    assert decision.allowed is False
    assert decision.reason == "BROKER_QUERY_UNAVAILABLE"


def test_t_l3_position_increase_after_release_freezes_further_orders():
    decision = validate_release_effect(
        before=_broker([_position(qty=1)]),
        after=_broker([_position(qty=2)]),
        symbol="TMFJ6",
    )
    assert decision.allowed is False
    assert decision.reason == "POST_RELEASE_POSITION_INCREASE"
    assert decision.audit["stop_further_orders"] is True


def test_t_l3_non_increasing_release_effect_is_allowed():
    decision = validate_release_effect(
        before=_broker([_position(qty=1)]),
        after=_broker([]),
        symbol="TMFJ6",
    )
    assert decision.allowed is True


def test_m1_m2_m3_mutation_guard_requires_specific_fail_closed_reasons():
    stale = _release({"kind": "HOLD_LEG", "leg": "near", "side": "SHORT", "qty": 1})
    assert evaluate_release_precondition(stale, _broker([])).reason == "BROKER_PREMISE_MISMATCH"
    assert evaluate_release_precondition(stale, _broker([], available=False)).reason == "BROKER_QUERY_UNAVAILABLE"


def test_m4_mutation_guard_post_effect_is_not_a_noop():
    result = validate_release_effect(
        before=_broker([_position(qty=1)]),
        after=_broker([_position(qty=2)]),
        symbol="TMFJ6",
    )
    assert result.audit["stop_further_orders"] is True


def test_m5_mutation_guard_startup_does_not_trust_stale_state():
    result = sanitize_startup_state(
        {"has_position": True, "near_side": "SHORT", "far_side": "LONG",
         "released_leg": "near", "near_entry": 1, "far_entry": 2},
        _broker([]), near_symbol="TMFJ6", far_symbol="TMFK6",
    )
    assert result.release_allowed is False
    assert result.state["near_side"] is None


def test_path_audit_release_guard_is_before_mts_release_create_order():
    source = Path("strategies/futures/monitor.py").read_text(encoding="utf-8")
    guard_at = source.index("_release_pre_submit_guard")
    release_create_at = source.index('strategy="MTS_RELEASE"')
    assert guard_at < release_create_at
    assert 'if _broker_pos > 0:\n                _near_side = "LONG"' not in source
