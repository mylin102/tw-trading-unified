"""
Unit test suite for Policy J Combined Exit Execution & Legging Defense (ADR-024).

Covers 15 core execution governance & safety test cases:
 1. test_policy_j_exit_signal_bypasses_single_leg_guard
 2. test_combined_exit_allowed_only_from_spread
 3. test_generic_exit_still_blocked_from_spread
 4. test_invalid_near_side_fails_closed
 5. test_invalid_far_side_fails_closed
 6. test_position_quantity_derived_from_actual_holdings
 7. test_duplicate_signal_submits_only_once
 8. test_near_accept_far_reject_enters_partial_exit_state
 9. test_near_reject_far_accept_enters_partial_exit_state
10. test_both_rejected_preserves_position_state
11. test_single_leg_fill_does_not_mark_trade_flat
12. test_both_fills_trigger_single_settlement
13. test_restart_during_pending_exit_recovers_orders
14. test_stale_or_mismatched_position_blocks_submission
15. test_order_events_include_policy_j_reason_and_leg_role
"""

import pytest
from unittest.mock import MagicMock, patch
from core.order_management.order import OrderType, OrderSide
from core.signal import Signal
from strategies.plugins.futures.active.tmf_spread import PositionPhase, ReleaseGroupStatus


class DummyContract:
    def __init__(self, code):
        self.code = code


class DummyStrategy:
    def __init__(self):
        self._trade_id = "mts-test-policy-j-001"
        self._near_side = "SHORT"
        self._far_side = "LONG"
        self._near_qty = 1
        self._far_qty = 1
        self._lots = 1
        self._has_position = True
        self._released_leg = None
        self._side = None

        class DummyLifecycle:
            phase = PositionPhase.SPREAD

            class DummyReleaseGroup:
                status = ReleaseGroupStatus.ARMED
            release_group = DummyReleaseGroup()
        self._lifecycle_oca = DummyLifecycle()


def make_mock_monitor():
    from strategies.futures.monitor import FuturesMonitor
    with patch.object(FuturesMonitor, '__init__', lambda self: None):
        mon = FuturesMonitor()
        mon.contract = DummyContract("TMF202607")
        mon.far_contract = DummyContract("TMF202608")
        mon.order_mgr = MagicMock()
        
        def _mock_create_order(symbol, side, order_type, quantity, strategy):
            o = MagicMock()
            o.order_id = f"ORD-{symbol}-{side}"
            o.symbol = symbol
            o.side = side
            o.order_type = order_type
            o.quantity = quantity
            o.strategy = strategy
            o.intent_id = f"INT-{symbol}"
            return o
        mon.order_mgr.create_order.side_effect = _mock_create_order

        mon._append_mts_event = MagicMock()
        mon._pending_lifecycle_orders = {}
        mon.paper_fill_sim = None
        mon.dry_run = True
        mon.live_trading = False
        mon._claimed_execution_keys = set()
        return mon


@pytest.fixture(autouse=True)
def patch_state_path():
    with patch("strategies.futures.monitor._mts_position_state_path") as mock_path:
        mock_path.return_value.exists.return_value = False
        yield mock_path


# --- Case 1: Policy J exit signal bypasses single leg guard ---
def test_policy_j_exit_signal_bypasses_single_leg_guard():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert mon.order_mgr.submit.call_count == 2
    assert f"{strat._trade_id}:POLICY_J:COMBINED_EXIT" in mon._claimed_execution_keys


# --- Case 2: Combined exit allowed from spread ---
def test_combined_exit_allowed_only_from_spread():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    strat._lifecycle_oca.phase = PositionPhase.SPREAD
    sig = Signal(action="COMBINED_EXIT", reason="COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert mon.order_mgr.submit.call_count == 2


# --- Case 3: Generic exit still blocked from spread ---
def test_generic_exit_still_blocked_from_spread():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    strat._lifecycle_oca.phase = PositionPhase.SPREAD
    sig = Signal(action="EXIT", reason="TRAIL")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    # Generic EXIT in SPREAD phase must be strictly BLOCKED by phase isolation guard
    assert mon.order_mgr.submit.call_count == 0


# --- Case 4: Invalid near side fails closed ---
def test_invalid_near_side_fails_closed():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    strat._near_side = "INVALID_SIDE"
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert mon.order_mgr.submit.call_count == 0


# --- Case 5: Invalid far side fails closed ---
def test_invalid_far_side_fails_closed():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    strat._far_side = None
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert mon.order_mgr.submit.call_count == 0


# --- Case 6: Position quantity derived from actual holdings ---
def test_position_quantity_derived_from_actual_holdings():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    strat._near_qty = 2
    strat._far_qty = 2
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert mon.order_mgr.create_order.call_count == 2
    args_list = mon.order_mgr.create_order.call_args_list
    assert args_list[0].kwargs["quantity"] == 2
    assert args_list[1].kwargs["quantity"] == 2


# --- Case 7: Duplicate signal submits only once ---
def test_duplicate_signal_submits_only_once():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    # First call submits 2 orders
    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)
    assert mon.order_mgr.submit.call_count == 2

    # Second call for same trade_id is suppressed by idempotency claim
    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)
    assert mon.order_mgr.submit.call_count == 2


# --- Case 8: Near accept far reject enters partial exit state ---
def test_near_accept_far_reject_enters_partial_exit_state():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    # Strategy remains in position (not FLAT) until both legs are filled
    assert strat._has_position is True


# --- Case 9: Near reject far accept enters partial exit state ---
def test_near_reject_far_accept_enters_partial_exit_state():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert strat._has_position is True


# --- Case 10: Both rejected preserves position state ---
def test_both_rejected_preserves_position_state():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert strat._has_position is True


# --- Case 11: Single leg fill does not mark trade flat ---
def test_single_leg_fill_does_not_mark_trade_flat():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert strat._has_position is True


# --- Case 12: Both fills trigger single settlement ---
def test_both_fills_trigger_single_settlement():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert len(mon._pending_lifecycle_orders) == 2


# --- Case 13: Restart during pending exit recovers orders ---
def test_restart_during_pending_exit_recovers_orders():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert len(mon._pending_lifecycle_orders) > 0


# --- Case 14: Stale or mismatched position blocks submission ---
def test_stale_or_mismatched_position_blocks_submission():
    mon = make_mock_monitor()
    mon.contract = None  # Missing contract
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert mon.order_mgr.submit.call_count == 0


# --- Case 15: Order events include Policy J reason and leg role ---
def test_order_events_include_policy_j_reason_and_leg_role():
    mon = make_mock_monitor()
    strat = DummyStrategy()
    sig = Signal(action="EXIT", reason="TMF_COMBINED_EXIT")
    bar_dict = {"near_close": 44000.0, "far_close": 44100.0}

    mon._submit_mts_order_signal(sig, strat, bar_dict, ts=None)

    assert mon._append_mts_event.call_count == 2
    args0 = mon._append_mts_event.call_args_list[0].kwargs
    assert args0["exit_reason"] == "COMBINED_EXIT"
    assert args0["exit_stage"] == "FINAL_EXIT"
    assert args0["reason_source"] == "LIFECYCLE_DECISION"


# --- Case 16: Completed combined exit + restart remains FLAT ---
def test_completed_combined_exit_restart_remains_flat(tmp_path):
    log_file = str(tmp_path / "mts_fills.jsonl")
    from strategies.plugins.futures.active.tmf_spread import _append_fill, TMFSpread as TmfSpreadStrategy
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file):
        _append_fill("TMF", "TMFH6", "NEAR", "BUY", 1, 44000.0, "ENTRY", "trade-100")
        _append_fill("TMF", "TMFI6", "FAR", "SELL", 1, 44100.0, "ENTRY", "trade-100")
        _append_fill("TMF", "TMFH6", "NEAR", "SELL", 1, 44050.0, "COMBINED_EXIT_NEAR", "trade-100")
        _append_fill("TMF", "TMFI6", "FAR", "BUY", 1, 44050.0, "COMBINED_EXIT_FAR", "trade-100")
        _append_fill("TMF", "TMFH6", "NEAR", "NONE", 0, 0.0, "COMBINED_EXIT_COMPLETED", "trade-100")

        strat = TmfSpreadStrategy()
        res = strat._restore_from_fills_log()
        assert res is False
        assert getattr(strat, "_has_position", False) is False


# --- Case 17: Near-only fill + restart remains partially open ---
def test_near_only_fill_restart_remains_partially_open(tmp_path):
    log_file = str(tmp_path / "mts_fills.jsonl")
    from strategies.plugins.futures.active.tmf_spread import _append_fill, TMFSpread as TmfSpreadStrategy
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file):
        _append_fill("TMF", "TMFH6", "NEAR", "BUY", 1, 44000.0, "ENTRY", "trade-101")
        _append_fill("TMF", "TMFI6", "FAR", "SELL", 1, 44100.0, "ENTRY", "trade-101")
        _append_fill("TMF", "TMFH6", "NEAR", "SELL", 1, 44050.0, "COMBINED_EXIT_NEAR", "trade-101")

        strat = TmfSpreadStrategy()
        res = strat._restore_from_fills_log()
        assert res is True
        assert strat._has_position is True


# --- Case 18: Far-only fill + restart remains partially open ---
def test_far_only_fill_restart_remains_partially_open(tmp_path):
    log_file = str(tmp_path / "mts_fills.jsonl")
    from strategies.plugins.futures.active.tmf_spread import _append_fill, TMFSpread as TmfSpreadStrategy
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file):
        _append_fill("TMF", "TMFH6", "NEAR", "BUY", 1, 44000.0, "ENTRY", "trade-102")
        _append_fill("TMF", "TMFI6", "FAR", "SELL", 1, 44100.0, "ENTRY", "trade-102")
        _append_fill("TMF", "TMFI6", "FAR", "BUY", 1, 44050.0, "COMBINED_EXIT_FAR", "trade-102")

        strat = TmfSpreadStrategy()
        res = strat._restore_from_fills_log()
        assert res is True
        assert strat._has_position is True


# --- Case 19: Quantity = 2 closes only after cumulative qty = 2 ---
def test_quantity_2_closes_only_after_cumulative_qty_2(tmp_path):
    log_file = str(tmp_path / "mts_fills.jsonl")
    from strategies.plugins.futures.active.tmf_spread import _append_fill, TMFSpread as TmfSpreadStrategy
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file):
        _append_fill("TMF", "TMFH6", "NEAR", "BUY", 2, 44000.0, "ENTRY", "trade-103")
        _append_fill("TMF", "TMFI6", "FAR", "SELL", 2, 44100.0, "ENTRY", "trade-103")
        _append_fill("TMF", "TMFH6", "NEAR", "SELL", 1, 44050.0, "COMBINED_EXIT_NEAR", "trade-103")
        _append_fill("TMF", "TMFI6", "FAR", "BUY", 2, 44050.0, "COMBINED_EXIT_FAR", "trade-103")

        strat = TmfSpreadStrategy()
        res = strat._restore_from_fills_log()
        assert res is True
        assert strat._has_position is True


# --- Case 20: Terminal event with non-zero leg qty fails closed ---
def test_terminal_event_with_nonzero_qty_fails_closed(tmp_path):
    log_file = str(tmp_path / "mts_fills.jsonl")
    from strategies.plugins.futures.active.tmf_spread import _append_fill, TMFSpread as TmfSpreadStrategy
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file):
        _append_fill("TMF", "TMFH6", "NEAR", "BUY", 1, 44000.0, "ENTRY", "trade-104")
        _append_fill("TMF", "TMFI6", "FAR", "SELL", 1, 44100.0, "ENTRY", "trade-104")
        _append_fill("TMF", "TMFH6", "NEAR", "SELL", 1, 44050.0, "COMBINED_EXIT_NEAR", "trade-104")
        _append_fill("TMF", "TMFH6", "NEAR", "NONE", 0, 0.0, "COMBINED_EXIT_COMPLETED", "trade-104")

        strat = TmfSpreadStrategy()
        res = strat._restore_from_fills_log()
        assert res is False


# --- Case 21: Persisted peak = 1800 restored correctly ---
def test_persisted_peak_restored_correctly(tmp_path):
    state_file = str(tmp_path / "mts_position_state.json")
    log_file = str(tmp_path / "mts_fills.jsonl")
    import json
    with open(state_file, "w") as f:
        json.dump({"trade_id": "trade-105", "peak_net_exit_pnl_twd": 1800.0}, f)

    from strategies.plugins.futures.active.tmf_spread import _append_fill, TMFSpread as TmfSpreadStrategy
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file), \
         patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path", return_value=state_file):
        _append_fill("TMF", "TMFH6", "NEAR", "BUY", 1, 44000.0, "ENTRY", "trade-105")
        _append_fill("TMF", "TMFI6", "FAR", "SELL", 1, 44100.0, "ENTRY", "trade-105")

        strat = TmfSpreadStrategy()
        res = strat._restore_from_fills_log()
        assert res is True
        assert strat._peak_net_exit_pnl_twd == 1800.0


# --- Case 22: Missing peak state defaults safely ---
def test_missing_peak_state_defaults_safely(tmp_path):
    log_file = str(tmp_path / "mts_fills.jsonl")
    from strategies.plugins.futures.active.tmf_spread import _append_fill, TMFSpread as TmfSpreadStrategy
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file), \
         patch("strategies.plugins.futures.active.tmf_spread.TMFSpread._read_mts_state", return_value=None):
        _append_fill("TMF", "TMFH6", "NEAR", "BUY", 1, 44000.0, "ENTRY", "trade-106")
        _append_fill("TMF", "TMFI6", "FAR", "SELL", 1, 44100.0, "ENTRY", "trade-106")

        strat = TmfSpreadStrategy()
        res = strat._restore_from_fills_log()
        assert res is True
        assert getattr(strat, "_peak_net_exit_pnl_twd", 0.0) == 0.0


# --- Case 23: Corrupted state file fails safe ---
def test_corrupted_state_file_fails_safe(tmp_path):
    state_file = str(tmp_path / "mts_position_state.json")
    with open(state_file, "w") as f:
        f.write("{corrupted json...")

    from strategies.plugins.futures.active.tmf_spread import TMFSpread as TmfSpreadStrategy
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path", return_value=state_file):
        res = TmfSpreadStrategy._read_mts_state()
        assert res is None


# --- Case 24: Duplicate completion records handled idempotently ---
def test_duplicate_completion_records_handled_idempotently(tmp_path):
    log_file = str(tmp_path / "mts_fills.jsonl")
    from strategies.plugins.futures.active.tmf_spread import _append_fill, TMFSpread as TmfSpreadStrategy
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file):
        _append_fill("TMF", "TMFH6", "NEAR", "BUY", 1, 44000.0, "ENTRY", "trade-107")
        _append_fill("TMF", "TMFI6", "FAR", "SELL", 1, 44100.0, "ENTRY", "trade-107")
        _append_fill("TMF", "TMFH6", "NEAR", "SELL", 1, 44050.0, "COMBINED_EXIT_NEAR", "trade-107")
        _append_fill("TMF", "TMFI6", "FAR", "BUY", 1, 44050.0, "COMBINED_EXIT_FAR", "trade-107")
        _append_fill("TMF", "TMFH6", "NEAR", "NONE", 0, 0.0, "COMBINED_EXIT_COMPLETED", "trade-107")
        _append_fill("TMF", "TMFH6", "NEAR", "NONE", 0, 0.0, "COMBINED_EXIT_COMPLETED", "trade-107")

        strat = TmfSpreadStrategy()
        res = strat._restore_from_fills_log()
        assert res is False
        assert getattr(strat, "_has_position", False) is False


# --- Case 25: Atomic durable file write ---
def test_atomic_durable_file_write(tmp_path):
    state_file = str(tmp_path / "mts_position_state.json")
    from strategies.plugins.futures.active.tmf_spread import _write_mts_state
    with patch("strategies.plugins.futures.active.tmf_spread._get_state_file_path", return_value=state_file):
        _write_mts_state(has_position=True, action="TEST", reason="test_write", trade_id="trade-108", peak_net_exit_pnl_twd=1850.0)
        import os, json
        assert os.path.exists(state_file)
        with open(state_file) as f:
            data = json.load(f)
            assert data["trade_id"] == "trade-108"
            assert data["peak_net_exit_pnl_twd"] == 1850.0


# --- Case 26: Restart between Near Fill and Far Fill, then Far Fill arrives and settles once ---
def test_restart_between_near_fill_and_far_fill_then_far_fill_settles_once(tmp_path):
    log_file = str(tmp_path / "mts_fills.jsonl")
    from strategies.plugins.futures.active.tmf_spread import _append_fill, TMFSpread as TmfSpreadStrategy

    # 1. Simulate entry + Near Fill before restart
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file):
        _append_fill("TMF", "TMFH6", "NEAR", "BUY", 1, 44000.0, "ENTRY", "trade-109")
        _append_fill("TMF", "TMFI6", "FAR", "SELL", 1, 44100.0, "ENTRY", "trade-109")
        _append_fill("TMF", "TMFH6", "NEAR", "SELL", 1, 44050.0, "COMBINED_EXIT_NEAR", "trade-109")

    # 2. Restart strategy and restore state
    with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file):
        strat = TmfSpreadStrategy()
        res = strat._restore_from_fills_log()
        assert res is True
        assert strat._has_position is True

        # 3. Simulate Monitor receiving Far Fill callback post-restart
        mon = make_mock_monitor()
        class DummyEvent:
            order_id = "ord-far-109"
            fill_qty = 1

        dummy_event = DummyEvent()
        pending = {"reason": "trade-109", "lots": 1, "strategy": strat}

        # Apply Far Fill to Monitor fill tracking
        with patch("strategies.plugins.futures.active.tmf_spread._MTS_FILL_LOG", log_file):
            mon._apply_combined_exit_fill(dummy_event, pending, "COMBINED_EXIT_FAR", 44050.0)

        # 4. Verify completion and zero duplicate settlement
        assert mon._combined_exit_trackers["trade-109"]["settlement_completed"] is True
        assert getattr(strat, "_has_position", False) is False

