"""Dashboard must expose broker-verified remaining MTS legs."""

from ui.dashboard import _build_live_broker_mts_state


def test_single_remaining_far_leg_is_not_flat():
    state = _build_live_broker_mts_state(
        [{
            "account": "futures",
            "code": "TMFI6",
            "quantity": 1,
            "direction": "Action.Buy",
            "avg_cost": 46021.0,
            "pnl": 770.0,
        }],
        {"canonical_input_hash": "abc123", "captured_at": 1786956218970},
        {"release_stop_points": 229.0, "trail_distance_points": 91.6},
    )
    assert state["has_position"] is True
    assert state["release_state"] == "SINGLE_LEG"
    assert state["released_leg"] == "near"
    assert state["near_status"] == "RELEASED"
    assert state["far_status"] == "OPEN"
    assert state["far_entry"] == 46021.0
    assert state["far_upl"] == 770.0
    assert state["near_upl"] is None


def test_invalid_or_empty_broker_positions_do_not_create_position():
    assert _build_live_broker_mts_state([], {}, {}) is None
    assert _build_live_broker_mts_state(
        [{"account": "futures", "code": "TMFI6", "quantity": 0}], {}, {}) is None
