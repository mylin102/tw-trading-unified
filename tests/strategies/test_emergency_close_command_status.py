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
