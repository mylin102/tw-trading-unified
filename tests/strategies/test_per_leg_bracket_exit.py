"""Contract tests for the opt-in independent per-leg bracket exits."""

from unittest.mock import patch

import pytest

import strategies.plugins.futures.active.tmf_spread as tmf
from strategies.plugins.futures.active.tmf_spread import TMFSpread


@pytest.fixture
def strategy():
    """Create the smallest open spread state needed by the bracket evaluator."""
    instance = TMFSpread()
    instance._has_position = True
    instance._near_entry = 100.0
    instance._far_entry = 200.0
    instance._near_side = "LONG"
    instance._far_side = "SHORT"
    instance._bracket_near_status = "ARMED"
    instance._bracket_far_status = "ARMED"
    instance._trade_id = "bracket-test"
    return instance


def _bar(*, near, far=200.0, near_source="LIVE_TICK", far_source="LIVE_TICK"):
    return {
        "near_close_rt": near,
        "far_close_rt": far,
        "near_price_source": near_source,
        "far_price_source": far_source,
    }


def test_switch_off_is_noop_and_does_not_change_leg_statuses(strategy, monkeypatch):
    monkeypatch.setattr(tmf, "PER_LEG_BRACKET_EXIT_ENABLED", False)

    with patch.object(tmf, "_append_event") as append_event:
        signal = strategy._check_per_leg_bracket_exit(_bar(near=160.0))

    assert signal is None
    assert strategy._bracket_near_status == "ARMED"
    assert strategy._bracket_far_status == "ARMED"
    append_event.assert_not_called()


@pytest.mark.parametrize(
    ("mark", "reason"),
    [(150.0, "BRACKET_TP"), (85.0, "BRACKET_SL")],
)
def test_long_near_leg_exits_on_first_tp_or_sl_cross(
    strategy, monkeypatch, mark, reason
):
    monkeypatch.setattr(tmf, "PER_LEG_BRACKET_EXIT_ENABLED", True)

    signal = strategy._check_per_leg_bracket_exit(_bar(near=mark))

    assert signal is not None
    assert signal.action == "PARTIAL_EXIT"
    assert signal.reason == reason
    assert strategy._bracket_near_status == "EXITED"
    assert strategy._bracket_far_status == "ARMED"


def test_short_leg_uses_mirrored_tp_threshold(strategy, monkeypatch):
    monkeypatch.setattr(tmf, "PER_LEG_BRACKET_EXIT_ENABLED", True)
    strategy._near_side = "SHORT"

    signal = strategy._check_per_leg_bracket_exit(_bar(near=50.0))

    assert signal is not None
    assert signal.action == "PARTIAL_EXIT"
    assert signal.reason == "BRACKET_TP"
    assert strategy._bracket_near_status == "EXITED"
    assert strategy._bracket_far_status == "ARMED"


def test_near_exit_leaves_far_armed_and_far_bracket_is_checked_next_bar(
    strategy, monkeypatch
):
    monkeypatch.setattr(tmf, "PER_LEG_BRACKET_EXIT_ENABLED", True)

    near_signal = strategy._check_per_leg_bracket_exit(_bar(near=150.0, far=200.0))

    assert near_signal is not None
    assert near_signal.action == "PARTIAL_EXIT"
    assert near_signal.reason == "BRACKET_TP"
    assert strategy._bracket_near_status == "EXITED"
    assert strategy._bracket_far_status == "ARMED"

    # The far leg remains independently evaluated on the next tick.
    far_signal = strategy._check_per_leg_bracket_exit(_bar(near=150.0, far=150.0))

    assert far_signal is not None
    assert far_signal.action == "EXIT"
    assert far_signal.reason == "BRACKET_TP"
    assert strategy._bracket_near_status == "EXITED"
    assert strategy._bracket_far_status == "EXITED"


def test_bar_close_only_near_mark_does_not_trigger_on_stale_price(
    strategy, monkeypatch
):
    monkeypatch.setattr(tmf, "PER_LEG_BRACKET_EXIT_ENABLED", True)

    # No near_close_rt: the otherwise TP-crossing near_close is BAR_CLOSE only.
    bar = {
        "near_close": 160.0,
        "far_close_rt": 200.0,
        "near_price_source": "BAR_CLOSE",
        "far_price_source": "LIVE_TICK",
    }
    signal = strategy._check_per_leg_bracket_exit(bar)

    assert signal is None
    assert strategy._bracket_near_status == "ARMED"
    assert strategy._bracket_far_status == "ARMED"


@pytest.fixture
def in_flight_strategy():
    """A position that predates the switch being flipped True: entered
    while PER_LEG_BRACKET_EXIT_ENABLED was False, so sync_position() never
    armed this leg (status stayed None). This simulates the exact state of
    a restored in-flight position after a mid-trade switch flip + restart
    (2026-11-20 user-directed special case)."""
    instance = TMFSpread()
    instance._has_position = True
    instance._near_entry = 100.0
    instance._far_entry = 200.0
    instance._near_side = "LONG"
    instance._far_side = "SHORT"
    instance._bracket_near_status = None
    instance._bracket_far_status = None
    instance._bracket_near_anchor = None
    instance._bracket_far_anchor = None
    instance._trade_id = "retroactive-test"
    return instance


def test_retroactive_arm_anchors_to_current_mark_not_original_entry(
    in_flight_strategy, monkeypatch
):
    monkeypatch.setattr(tmf, "PER_LEG_BRACKET_EXIT_ENABLED", True)

    with patch.object(tmf, "_append_event") as append_event:
        # Current live mark (130.0) is far from original entry (100.0).
        signal = in_flight_strategy._check_per_leg_bracket_exit(
            _bar(near=130.0, far=200.0)
        )

    # Arming alone must not itself fire an exit on the same bar.
    assert signal is None
    assert in_flight_strategy._bracket_near_status == "ARMED"
    assert in_flight_strategy._bracket_near_anchor == 130.0
    append_event.assert_any_call(
        "PER_LEG_BRACKET_RETROACTIVE_ARM",
        trade_id="retroactive-test",
        leg="NEAR",
        anchor_price=130.0,
        original_entry_price=100.0,
    )

    # 100.0 is between anchor-SL(115.0) and anchor-TP(180.0): must NOT fire
    # if anchor(130.0) is used. If entry(100.0) were wrongly used as anchor,
    # this exact price would sit ON the entry price, proving nothing; use a
    # price below the anchor-SL instead to positively discriminate.
    signal2 = in_flight_strategy._check_per_leg_bracket_exit(
        _bar(near=100.0, far=200.0)
    )
    assert signal2 is not None
    assert signal2.reason == "BRACKET_SL"
    assert in_flight_strategy._bracket_near_status == "EXITED"


def test_retroactive_arm_does_not_fire_against_stale_bar_close_mark(
    in_flight_strategy, monkeypatch
):
    monkeypatch.setattr(tmf, "PER_LEG_BRACKET_EXIT_ENABLED", True)

    bar = {
        "near_close": 130.0,
        "far_close_rt": 200.0,
        "near_price_source": "BAR_CLOSE",
        "far_price_source": "LIVE_TICK",
    }
    signal = in_flight_strategy._check_per_leg_bracket_exit(bar)

    assert signal is None
    assert in_flight_strategy._bracket_near_status is None
    assert in_flight_strategy._bracket_near_anchor is None


def test_retroactive_arm_is_independent_per_leg(in_flight_strategy, monkeypatch):
    monkeypatch.setattr(tmf, "PER_LEG_BRACKET_EXIT_ENABLED", True)

    in_flight_strategy._check_per_leg_bracket_exit(_bar(near=130.0, far=250.0))

    assert in_flight_strategy._bracket_near_status == "ARMED"
    assert in_flight_strategy._bracket_near_anchor == 130.0
    assert in_flight_strategy._bracket_far_status == "ARMED"
    assert in_flight_strategy._bracket_far_anchor == 250.0
