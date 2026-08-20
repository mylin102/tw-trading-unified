"""Dashboard must expose broker-verified remaining MTS legs."""

from ui.dashboard import _build_live_broker_mts_state, _mts_direction_label


def test_paper_mts_direction_falls_back_to_leg_sides():
    assert _mts_direction_label({
        "near_side": "SHORT", "far_side": "LONG"
    }) == "SHORT / LONG"
    assert _mts_direction_label({"action": "BUY_NEAR_SELL_FAR",
                                 "near_side": "SHORT", "far_side": "LONG"}) \
        == "BUY_NEAR_SELL_FAR"


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

def test_pending_live_mts_exit_orders_are_deduplicated_and_display_only():
    from ui.dashboard import _pending_live_mts_exit_orders

    rows = _pending_live_mts_exit_orders({
        "fetch_status": {"capture": "OK"},
        "open_orders": [
            {"code": "TMFI6", "status": "PendingSubmit",
             "broker_order_id": "broker-1", "seqno": "7", "direction": "sell"},
            {"code": "TMFI6", "status": "PendingSubmit",
             "broker_order_id": "broker-1", "seqno": "7", "direction": "sell"},
        ],
    })
    assert rows == [{"商品": "TMFI6", "方向": "sell",
                     "券商委託": "broker-1", "狀態": "PENDINGSUBMIT"}]
    assert _pending_live_mts_exit_orders({"fetch_status": {"capture": "FAIL"},
                                          "open_orders": rows}) == []
