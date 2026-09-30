"""Direction-normalization contract for core.futures_release_guard.

Regression: shioaji 1.5.5 FuturePosition.direction stringifies to
'Action.Buy'/'Action.Sell'; the snapshot carries that raw string and
evaluate_release_precondition must map it to LONG/SHORT, otherwise every
HOLD_LEG premise fails (BROKER_PREMISE_MISMATCH) and releases are frozen.
"""
from core.futures_release_guard import (
    ReleaseIntent,
    evaluate_release_precondition,
)


def _snapshot(positions):
    return {
        "source": "live_broker",
        "fetch_status": {"capture": "OK"},
        "positions": positions,
    }


def _intent(symbol, premise_side):
    return ReleaseIntent(
        action="PARTIAL_EXIT",
        symbol=symbol,
        side="SELL" if premise_side == "LONG" else "BUY",
        qty=1,
        premise={"kind": "HOLD_LEG", "leg": "near", "side": premise_side, "qty": 1},
    )


def test_hold_leg_accepts_action_buy_direction():
    snap = _snapshot([
        {"account": "futures", "code": "TMFJ6", "quantity": 1,
         "direction": "Action.Buy", "avg_cost": 48605.0},
    ])
    d = evaluate_release_precondition(_intent("TMFJ6", "LONG"), snap)
    assert d.allowed is True, d.reason
    assert d.reason == "BROKER_PREMISE_CONFIRMED"


def test_hold_leg_accepts_action_sell_direction():
    snap = _snapshot([
        {"account": "futures", "code": "TMFK6", "quantity": 1,
         "direction": "Action.Sell", "avg_cost": 48754.0},
    ])
    d = evaluate_release_precondition(_intent("TMFK6", "SHORT"), snap)
    assert d.allowed is True, d.reason


def test_hold_leg_accepts_bare_enum_name_and_canonical_forms():
    for direction in ("Buy", "SELL", "LONG", "SHORT"):
        side = "LONG" if direction in ("Buy", "LONG") else "SHORT"
        snap = _snapshot([
            {"account": "futures", "code": "TMFJ6", "quantity": 1,
             "direction": direction},
        ])
        d = evaluate_release_precondition(_intent("TMFJ6", side), snap)
        assert d.allowed is True, f"{direction} -> {d.reason}"


def test_hold_leg_still_fails_closed_on_unknown_direction():
    snap = _snapshot([
        {"account": "futures", "code": "TMFJ6", "quantity": 1,
         "direction": "Weird", "avg_cost": 48605.0},
    ])
    d = evaluate_release_precondition(_intent("TMFJ6", "LONG"), snap)
    assert d.allowed is False
    assert d.reason == "BROKER_PREMISE_MISMATCH"


def test_hold_leg_still_fails_closed_on_missing_target():
    snap = _snapshot([
        {"account": "futures", "code": "TMFK6", "quantity": 1,
         "direction": "Action.Sell"},
    ])
    d = evaluate_release_precondition(_intent("TMFJ6", "LONG"), snap)
    assert d.allowed is False
    assert d.reason == "BROKER_PREMISE_MISMATCH"
