import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.order_management.order import OrderStatus
from strategies.futures.monitor import FuturesMonitor


def _monitor_with_tracker(order_ids=("ORD-NEAR", "ORD-FAR")):
    with patch.object(FuturesMonitor, "__init__", lambda self: None):
        monitor = FuturesMonitor()
    monitor._write_manual_command_status = MagicMock()
    monitor._emergency_cmd = {
        "command_id": "CMD-TEST",
        "order_ids": set(order_ids),
        "filled_ids": set(),
        "fill_prices": {},
        "completed": False,
        "failed": False,
    }
    return monitor


def _event(order_id, status=OrderStatus.FILLED, price=44000.0, reason="test"):
    return SimpleNamespace(order_id=order_id, status=status, fill_price=price, reason=reason)


def test_emergency_close_stays_processing_until_all_expected_orders_fill():
    monitor = _monitor_with_tracker()

    monitor._maybe_complete_emergency_command(_event("ORD-NEAR", price=44001), 44001)
    monitor._write_manual_command_status.assert_not_called()

    monitor._maybe_complete_emergency_command(_event("ORD-FAR", price=44101), 44101)
    monitor._write_manual_command_status.assert_called_once_with(
        "CMD-TEST", "COMPLETED", "平倉已成交",
        position_after={"near_qty": 0, "far_qty": 0},
        order_ids=["ORD-FAR", "ORD-NEAR"],
        fill_prices={"ORD-FAR": 44101.0, "ORD-NEAR": 44001.0},
    )


def test_emergency_close_single_remaining_leg_completes_on_its_fill():
    monitor = _monitor_with_tracker(("ORD-NEAR",))

    monitor._maybe_complete_emergency_command(_event("ORD-NEAR"), 44000)

    assert monitor._write_manual_command_status.call_args.args[:3] == (
        "CMD-TEST", "COMPLETED", "平倉已成交"
    )


def test_emergency_close_reject_is_terminal_and_ignores_later_fill():
    monitor = _monitor_with_tracker()

    monitor._fail_emergency_command(_event("ORD-FAR", status=OrderStatus.REJECTED, reason="broker reject"), "REJECTED")
    monitor._maybe_complete_emergency_command(_event("ORD-NEAR"), 44000)
    monitor._maybe_complete_emergency_command(_event("ORD-FAR"), 44100)

    assert monitor._write_manual_command_status.call_count == 1
    assert monitor._write_manual_command_status.call_args.args[:3] == (
        "CMD-TEST", "FAILED", "平倉單 REJECTED: ORD-FAR (broker reject)"
    )


def test_unrelated_order_cannot_complete_or_fail_emergency_command():
    monitor = _monitor_with_tracker()

    monitor._maybe_complete_emergency_command(_event("ORD-OTHER"), 44000)
    monitor._fail_emergency_command(_event("ORD-OTHER", status=OrderStatus.REJECTED), "REJECTED")

    monitor._write_manual_command_status.assert_not_called()


# ── 2026-08-06 Hermes Agent P1: close_all fail-closed on invalid sides ──
# BROKER_RECONCILED once wrote leg labels ("NEAR"/"FAR") into near_side/far_side;
# the old `SELL if == "LONG" else BUY` mapping silently sent BUY for ANY
# non-LONG value (wrong direction for a LONG far leg). These tests pin the
# fail-closed behaviour: invalid sides → FAILED status, zero orders.

def _monitor_for_close_all(state_json, tmpd):
    with patch.object(FuturesMonitor, "__init__", lambda self: None):
        monitor = FuturesMonitor()
    monitor._write_manual_command_status = MagicMock()
    monitor._cancel_all_pending_orders = MagicMock()
    monitor._register_emergency_command_order = MagicMock()
    monitor._save_orders_file_wrapper = MagicMock()
    monitor._registry = MagicMock()
    monitor.cfg = {"mts": {}}
    monitor.order_mgr = MagicMock(active_orders={})
    monitor.paper_fill_sim = MagicMock()
    monitor.market_data = {}
    monitor._tick_bars_deque = []
    monitor._pending_lifecycle_orders = {}
    monitor._processed_flag_ids = set()
    monitor._flag_retry_count = 0
    monitor._manual_trade_status = ""
    monitor._lifecycle_generation = 0
    monitor._emergency_reset_at = None
    monitor.ticker = "TMF"
    monitor.contract = SimpleNamespace(code="TMFH6")
    monitor.far_contract = SimpleNamespace(code="TMFI6")
    monitor.trader = MagicMock(position=1)
    state_path = Path(tmpd) / "state.json"
    state_path.write_text(json.dumps(state_json))
    flag_path = os.path.join(tmpd, "flag.json")
    with open(flag_path, "w") as f:
        json.dump({"action": "close_all", "command_id": "CMD-FAILTEST", "created_at": time.time()}, f)
    monitor.manual_trade_flag_path = flag_path
    return monitor, state_path


def _run_close_all(monitor, state_path):
    with patch("strategies.futures.monitor._mts_position_state_path", return_value=state_path), \
         patch("strategies.plugins.futures.active.tmf_spread.settle_mts_trade"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        monitor._process_manual_trade_flag()


def test_close_all_fails_closed_on_invalid_sides():
    tmpd = tempfile.mkdtemp()
    try:
        monitor, state_path = _monitor_for_close_all({
            "has_position": True, "near_side": "NEAR", "far_side": "FAR",
            "near_entry": 44251.0, "far_entry": 44177.0, "trade_id": "mts-test",
        }, tmpd)
        _run_close_all(monitor, state_path)
        calls = monitor._write_manual_command_status.call_args_list
        failed = [c for c in calls if len(c.args) >= 2 and c.args[1] == "FAILED"]
        assert failed, f"no FAILED status written; calls={[c.args[:2] for c in calls]}"
        assert "invalid side" in failed[-1].args[2]
        monitor.order_mgr.create_order.assert_not_called()
        monitor.order_mgr.submit.assert_not_called()
        assert not os.path.exists(monitor.manual_trade_flag_path + ".processing"), \
            "terminal FAILED must remove .processing (no retry loop)"
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def test_close_all_fails_closed_on_invalid_remaining_leg_side():
    tmpd = tempfile.mkdtemp()
    try:
        monitor, state_path = _monitor_for_close_all({
            "has_position": True, "near_side": "SHORT", "far_side": "FAR",
            "released_leg": "near", "near_entry": 44251.0, "far_entry": 44177.0,
            "trade_id": "mts-test",
        }, tmpd)
        _run_close_all(monitor, state_path)
        calls = monitor._write_manual_command_status.call_args_list
        assert any(len(c.args) >= 2 and c.args[1] == "FAILED" for c in calls), \
            f"no FAILED status; calls={[c.args[:2] for c in calls]}"
        monitor.order_mgr.create_order.assert_not_called()
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)


def test_close_all_submits_correct_directions_with_valid_sides():
    tmpd = tempfile.mkdtemp()
    try:
        monitor, state_path = _monitor_for_close_all({
            "has_position": True, "near_side": "SHORT", "far_side": "LONG",
            "near_entry": 44251.0, "far_entry": 44177.0, "trade_id": "mts-test",
        }, tmpd)
        _run_close_all(monitor, state_path)
        from core.order_management.order import OrderSide
        sides = [c.kwargs["side"] for c in monitor.order_mgr.create_order.call_args_list]
        assert sides == [OrderSide.BUY, OrderSide.SELL], \
            f"near SHORT→BUY, far LONG→SELL; got {sides}"
        assert not any(len(c.args) >= 2 and c.args[1] == "FAILED"
                       for c in monitor._write_manual_command_status.call_args_list)
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)
