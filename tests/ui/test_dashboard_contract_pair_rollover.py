"""RED/GREEN: the MTS panel must not render FLAT when the contract pair rolls.

2026-09-24: the broker held TMFJ6 (near) / TMFK6 (far) while the dashboard
matched only the literal pair {"TMFH6", "TMFI6"} (Aug/Sep) -> the filter
returned no legs -> _broker_snapshot_flat -> a real two-leg position was
displayed as FLAT.  Contract months roll every month, so near/far must be
derived from the broker snapshot itself.
"""
from ui.dashboard import (
    _build_live_broker_mts_state,
    _pending_live_mts_exit_orders,
)

_ROLLED = [
    {"account": "futures", "code": "TMFJ6", "quantity": 1,
     "direction": "Action.Sell", "avg_cost": 48176.0, "pnl": 2460.0},
    {"account": "futures", "code": "TMFK6", "quantity": 1,
     "direction": "Action.Buy", "avg_cost": 48320.0, "pnl": -2400.0},
]


def test_rolled_pair_renders_both_held_not_flat():
    # wire order must not decide which leg is near
    state = _build_live_broker_mts_state(
        list(reversed(_ROLLED)),
        {"canonical_input_hash": "b44e20763a980f92",
         "captured_at": 1790260632973},
        {"release_stop_points": 109.0, "trail_distance_points": 43.6},
    )
    assert state["has_position"] is True
    assert state["release_state"] == "BOTH_HELD"
    assert state["near_entry"] == 48176.0
    assert state["far_entry"] == 48320.0
    assert state["near_side"] == "Action.Sell"
    assert state["far_side"] == "Action.Buy"
    assert state["near_upl"] == 2460.0
    assert state["far_upl"] == -2400.0


def test_two_legs_across_a_year_boundary_are_ordered_by_month():
    state = _build_live_broker_mts_state(
        [{"account": "futures", "code": "TMFA7", "quantity": 1,
          "direction": "Action.Sell", "avg_cost": 100.0, "pnl": 1.0},
         {"account": "futures", "code": "TMFL6", "quantity": 1,
          "direction": "Action.Buy", "avg_cost": 200.0, "pnl": 2.0}],
        {}, {})
    assert state["near_entry"] == 200.0     # TMFL6 = Dec 2026 is nearer
    assert state["far_entry"] == 100.0      # TMFA7 = Jan 2027


def test_single_remaining_leg_uses_the_reconciled_pair():
    state = _build_live_broker_mts_state(
        [{"account": "futures", "code": "TMFK6", "quantity": 1,
          "direction": "Action.Buy", "avg_cost": 48320.0, "pnl": -2400.0}],
        {"canonical_input_hash": "x", "captured_at": 1},
        {},
        pair_codes=["TMFJ6", "TMFK6"],
    )
    assert state["has_position"] is True
    assert state["release_state"] == "SINGLE_LEG"
    assert state["released_leg"] == "near"
    assert state["far_status"] == "OPEN"
    assert state["far_entry"] == 48320.0


def test_single_leg_without_pair_never_fabricates_a_release():
    state = _build_live_broker_mts_state(
        [{"account": "futures", "code": "TMFK6", "quantity": 1,
          "direction": "Action.Buy", "avg_cost": 48320.0, "pnl": -2400.0}],
        {}, {})
    assert state["has_position"] is True     # never FLAT
    assert state["release_state"] == "SINGLE_LEG"
    assert state["released_leg"] is None


def test_pending_exit_orders_accept_the_rolled_pair():
    rows = _pending_live_mts_exit_orders({
        "fetch_status": {"capture": "OK"},
        "open_orders": [
            {"code": "TMFJ6", "status": "PendingSubmit",
             "broker_order_id": "b1", "seqno": "1", "direction": "buy"},
            {"code": "2330", "status": "PendingSubmit",
             "broker_order_id": "b2", "seqno": "2", "direction": "buy"},
        ],
    })
    assert [r["商品"] for r in rows] == ["TMFJ6"]


def test_non_futures_or_empty_legs_are_not_a_position():
    assert _build_live_broker_mts_state(
        [{"account": "stock", "code": "TMFJ6", "quantity": 1}],
        {}, {}) is None
    assert _build_live_broker_mts_state(
        [{"account": "futures", "code": "TMFK6", "quantity": 0}],
        {}, {}) is None
