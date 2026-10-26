"""Contract tests for the MTS opening-window combined-exit behavior."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from core.order_management.order import Order, OrderSide, OrderStatus, OrderType
from core.signal import Signal
from strategies.futures.monitor import FuturesMonitor
from strategies.plugins.futures.active.tmf_spread import PositionPhase


# 2026-10-26 Hermes Agent: Keep 08:45-09:05 COMBINED_EXIT order and cancel
# behavior pinned without changing production or existing test contracts.


class _DummyContract:
    def __init__(self, code):
        self.code = code


class _DummyStrategy:
    def __init__(self):
        self._trade_id = "mts-0905-window-test"
        self._near_side = "SHORT"
        self._far_side = "LONG"
        self._near_qty = self._far_qty = self._lots = 1
        self._has_position = True
        self._released_leg = None
        self._side = None
        self._held_near_contract = _DummyContract("TMF202607")
        self._held_far_contract = _DummyContract("TMF202608")

        class _Lifecycle:
            phase = PositionPhase.SPREAD

            class _ReleaseGroup:
                status = "armed"

            release_group = _ReleaseGroup()

        self._lifecycle_oca = _Lifecycle()


@pytest.fixture
def monitor_and_strategy(tmp_path, monkeypatch):
    return _build_monitor(tmp_path, monkeypatch)


def _build_monitor(tmp_path, monkeypatch):
    import strategies.futures.monitor as monitor_module
    import strategies.plugins.futures.active.tmf_spread as tmf_spread_module

    monkeypatch.setattr(monitor_module, "_mts_intent_log_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        monitor_module, "_mts_position_state_path", lambda: tmp_path / "mts_state.json"
    )
    monkeypatch.setattr(
        monitor_module, "is_taifex_futures_market_open", lambda: True
    )
    monkeypatch.setattr(tmf_spread_module, "_write_mts_state", MagicMock())

    with patch.object(FuturesMonitor, "__init__", lambda self: None):
        mon = FuturesMonitor()
    mon.contract = _DummyContract("TMF202607")
    mon.far_contract = _DummyContract("TMF202608")
    mon.order_mgr = MagicMock()
    mon.paper_fill_sim = None
    mon.dry_run = True
    mon.live_trading = False
    mon._claimed_execution_keys = set()
    mon._pending_lifecycle_orders = {}
    mon._save_orders_file_wrapper = MagicMock()
    mon._append_mts_event = MagicMock()
    mon.order_mgr.create_order.reset_mock()
    created = []

    def create_order(**kwargs):
        order = MagicMock()
        for key, value in kwargs.items():
            setattr(order, key, value)
        order.order_id = f"ORDER-{len(created) + 1}"
        order.intent_id = f"INTENT-{len(created) + 1}"
        order.client_order_id = None
        order.quarantine_exit_proof = None
        created.append((kwargs, order))
        return order

    mon.order_mgr.create_order.side_effect = create_order
    strat = _DummyStrategy()
    strat._lifecycle_oca.phase = PositionPhase.SPREAD
    return mon, strat, created


@pytest.mark.parametrize(
    ("now", "expected_type", "expected_prices"),
    [
        (datetime(2026, 10, 26, 8, 44, 59), OrderType.MKP, (None, None)),
        (datetime(2026, 10, 26, 8, 45, 0), OrderType.LIMIT, (44000.0, 44100.0)),
        (datetime(2026, 10, 26, 9, 4, 59), OrderType.LIMIT, (44000.0, 44100.0)),
        (datetime(2026, 10, 26, 9, 5, 0), OrderType.MKP, (None, None)),
    ],
)
def test_combined_exit_window_selects_limit_prices_only_inside_window(
    monitor_and_strategy, now, expected_type, expected_prices
):
    mon, strat, created = monitor_and_strategy

    mon._submit_mts_order_signal(
        Signal(action="COMBINED_EXIT", reason="COMBINED_EXIT"),
        strat,
        {"near_close": 44000.0, "far_close": 44100.0},
        now,
    )

    assert len(created) == 2
    assert [entry[0]["order_type"] for entry in created] == [expected_type] * 2
    if expected_type is OrderType.LIMIT:
        assert [entry[0]["price"] for entry in created] == list(expected_prices)
    else:
        assert all("price" not in entry[0] for entry in created)


class _RecordingPaperFillSim:
    def __init__(self):
        self._pending_orders = {}
        self.processed_ticks = []

    def register(self, order):
        self._pending_orders[order.order_id] = order

    def process_tick(self, tick):
        self.processed_ticks.append(tick)
        for order_id, order in list(self._pending_orders.items()):
            if order.symbol == tick.symbol:
                self._pending_orders.pop(order_id)


@pytest.mark.parametrize(
    ("now", "expected_pending", "expected_ticks"),
    [
        (datetime(2026, 10, 26, 8, 50), True, 0),
        (datetime(2026, 10, 26, 9, 5), False, 2),
    ],
)
def test_paper_combined_exit_stays_pending_only_inside_window(
    monitor_and_strategy, now, expected_pending, expected_ticks
):
    mon, strat, _created = monitor_and_strategy
    mon.paper_fill_sim = _RecordingPaperFillSim()
    mon._make_synthetic_tick = lambda price, ts, symbol: SimpleNamespace(
        symbol=symbol, close=price, datetime=ts
    )

    mon._submit_mts_order_signal(
        Signal(action="COMBINED_EXIT", reason="COMBINED_EXIT"),
        strat,
        {"near_close": 44000.0, "far_close": 44100.0},
        now,
    )

    if expected_pending:
        assert len(mon.paper_fill_sim._pending_orders) == 2
    else:
        assert mon.paper_fill_sim._pending_orders == {}
    assert len(mon.paper_fill_sim.processed_ticks) == expected_ticks


class _RecordingOrderManager:
    def __init__(self, orders, simulator):
        self.active_orders = {order.order_id: order for order in orders}
        self.simulator = simulator
        self.cancel_calls = []

    def cancel(self, order_id, *, reason, source):
        self.cancel_calls.append((order_id, reason, source))
        order = self.active_orders[order_id]
        if order.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }:
            raise ValueError(f"terminal order: {order.status.value}")
        order.status = OrderStatus.CANCELLED
        self.active_orders.pop(order_id)
        self.simulator._pending_orders.pop(order_id, None)
        return True


def _cancel_monitor(near_status=OrderStatus.SUBMITTED):
    mon = FuturesMonitor.__new__(FuturesMonitor)
    near = Order(
        "TMF202607", OrderSide.SELL, OrderType.LIMIT, 1, 44000.0,
        order_id="NEAR-ORDER", strategy="MTS_EXIT",
    )
    far = Order(
        "TMF202608", OrderSide.BUY, OrderType.LIMIT, 1, 44100.0,
        order_id="FAR-ORDER", strategy="MTS_EXIT",
    )
    near.status = near_status
    far.status = OrderStatus.SUBMITTED
    sim = _RecordingPaperFillSim()
    sim._pending_orders = {far.order_id: far}
    if near_status is not OrderStatus.FILLED:
        sim._pending_orders[near.order_id] = near
    mon.paper_fill_sim = sim
    mon.order_mgr = _RecordingOrderManager((near, far), sim)
    mon._claimed_execution_keys = {"trade-0905:POLICY_J:COMBINED_EXIT"}
    mon._pending_lifecycle_orders = {
        near.order_id: {"leg_role": "NEAR"},
        far.order_id: {"leg_role": "FAR"},
    }
    mon._append_mts_event = MagicMock()
    mon._mts_0905_pending_exits = {
        "GROUP-0905": {
            "trade_id": "trade-0905",
            "claim_key": "trade-0905:POLICY_J:COMBINED_EXIT",
            "near_order_id": near.order_id,
            "far_order_id": far.order_id,
        }
    }
    return mon, near, far


def test_0905_sweep_cancels_both_pending_legs_and_releases_claim():
    mon, near, far = _cancel_monitor()

    mon._mts_0905_cancel_unfilled_exits(datetime(2026, 10, 26, 9, 5))

    assert mon.order_mgr.cancel_calls == [
        (near.order_id, "MTS_0905_WINDOW_EXPIRED", "MTS_0905_CANCEL"),
        (far.order_id, "MTS_0905_WINDOW_EXPIRED", "MTS_0905_CANCEL"),
    ]
    assert near.order_id not in mon.paper_fill_sim._pending_orders
    assert far.order_id not in mon.paper_fill_sim._pending_orders
    assert "trade-0905:POLICY_J:COMBINED_EXIT" not in mon._claimed_execution_keys
    assert mon._mts_0905_pending_exits == {}
    assert mon._append_mts_event.call_count == 2
    assert all(
        call.args[0] == "COMBINED_EXIT_0905_CANCELLED"
        for call in mon._append_mts_event.call_args_list
    )


def test_0905_sweep_before_cutoff_is_noop():
    mon, near, far = _cancel_monitor()

    mon._mts_0905_cancel_unfilled_exits(datetime(2026, 10, 26, 9, 4, 59))

    assert mon.order_mgr.cancel_calls == []
    assert set(mon.paper_fill_sim._pending_orders) == {near.order_id, far.order_id}
    assert "trade-0905:POLICY_J:COMBINED_EXIT" in mon._claimed_execution_keys
    assert "GROUP-0905" in mon._mts_0905_pending_exits
    mon._append_mts_event.assert_not_called()


def test_0905_sweep_preserves_filled_leg_and_cancels_only_pending_leg():
    mon, near, far = _cancel_monitor(near_status=OrderStatus.FILLED)

    mon._mts_0905_cancel_unfilled_exits(datetime(2026, 10, 26, 9, 5))

    assert mon.order_mgr.cancel_calls == [
        (far.order_id, "MTS_0905_WINDOW_EXPIRED", "MTS_0905_CANCEL")
    ]
    assert mon.order_mgr.active_orders[near.order_id] is near
    assert near.order_id not in mon.paper_fill_sim._pending_orders
    assert near.status is OrderStatus.FILLED
    assert far.order_id not in mon.paper_fill_sim._pending_orders
    assert "trade-0905:POLICY_J:COMBINED_EXIT" not in mon._claimed_execution_keys
    assert mon._mts_0905_pending_exits == {}
    assert mon._append_mts_event.call_count == 1
    assert mon._append_mts_event.call_args.args == (
        "COMBINED_EXIT_0905_CANCELLED",
    )
    assert mon._append_mts_event.call_args.kwargs["leg_role"] == "FAR"
