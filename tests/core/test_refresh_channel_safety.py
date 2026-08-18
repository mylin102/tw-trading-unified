"""Deterministic timeout channel-safety contracts.

These tests intentionally exercise the production refresh transaction with
threading.Event barriers.  They do not sleep to guess worker timing and do
not call a broker.
"""

from types import SimpleNamespace
import threading

import pytest


def _monitor(api, timeout=0.01):
    from strategies.futures.monitor import FuturesMonitor

    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon._broker_refresh_lock = threading.Lock()
    mon._futures_refresh_generation = 0
    mon._futures_refresh_timeout_s = timeout
    mon._last_futures_refresh = None
    mon._append_mts_event = lambda *args, **kwargs: None
    mon.api = api
    return mon


class ControlledRefreshAPI:
    is_connected = True
    futopt_account = object()

    def __init__(self):
        self.a_started = threading.Event()
        self.a_release = threading.Event()
        self.a_completed = threading.Event()
        self.b_started = threading.Event()
        self.b_completed = threading.Event()
        self.calls = []
        self.list_calls = 0

    def update_status(self, **kwargs):
        self.calls.append(("update", kwargs))
        if len([c for c in self.calls if c[0] == "update"]) == 1:
            self.a_started.set()
            self.a_release.wait()
            self.a_completed.set()
        else:
            self.b_started.set()
            self.b_completed.set()

    def list_trades(self):
        self.calls.append(("list", {}))
        self.list_calls += 1
        return []


def test_timeout_late_worker_requires_fresh_b_and_never_publishes_a(monkeypatch):
    """A late A completion cannot clear uncertainty or publish generation."""
    api = ControlledRefreshAPI()
    mon = _monitor(api)
    generation_ids = iter((1001, 2002))
    monkeypatch.setattr(
        "strategies.futures.monitor.time.time_ns", lambda: next(generation_ids))

    first = mon._refresh_futures_trade_view(api, api.futopt_account)
    assert api.a_started.is_set()
    assert first["state"] == "REFRESH_TIMEOUT_UNCERTAIN"
    assert first["snapshot_generation"] is None
    assert mon._last_futures_refresh["state"] == "REFRESH_TIMEOUT_UNCERTAIN"

    api.a_release.set()
    assert api.a_completed.wait(1.0)
    # The late worker has completed, but it did not publish or heal the channel.
    assert mon._last_futures_refresh["state"] == "REFRESH_TIMEOUT_UNCERTAIN"
    assert api.list_calls == 1

    second = mon._refresh_futures_trade_view(api, api.futopt_account)
    assert api.b_started.is_set() and api.b_completed.is_set()
    assert second["state"] == "REFRESH_SUCCEEDED"
    # A timed-out A never consumes a generation token; B gets the first
    # published generation only after its own successful update/list pair.
    assert second["snapshot_generation"] == "futures-1001-1"
    assert mon._last_futures_refresh["snapshot_generation"] == "futures-1001-1"
    assert [kind for kind, _ in api.calls] == ["update", "list", "update", "list"]


def test_timeout_blocks_all_order_channels_and_preserves_pending_lock(monkeypatch):
    """Timeout uncertainty must fail closed for all order-capable channels."""
    from core.mode_transition import ExecutionContext, ModeTransitionState
    from core.order_management.order import OrderSide, OrderType
    from core.order_management.order_manager import OrderManager

    api = ControlledRefreshAPI()
    mon = _monitor(api)
    mon._execution_context = ExecutionContext(
        requested_mode="live",
        effective_mode=ModeTransitionState.LIVE_READY.value,
        live_order_allowed=True,
    )
    mon._live_broker_flat_proven = True
    mon._broker_authority_degraded = False
    mon._broker_position_observed = False
    sends = []
    adapter = SimpleNamespace(place_order=lambda *a, **k: sends.append((a, k)))
    manager = OrderManager(mode="live", broker_adapter=adapter,
                           execution_context=mon._execution_context)
    mon.order_mgr = manager
    mon._capture_post_startup_snapshot = lambda: {
        "fetch_status": {"capture": "FAIL"},
        "open_orders": [], "positions": [],
    }
    strategy = SimpleNamespace(_has_position=False)
    mon._refresh_futures_trade_view(api, api.futopt_account)
    api.a_release.set()
    assert api.a_completed.wait(1.0)
    assert mon._last_futures_refresh["state"] == "REFRESH_TIMEOUT_UNCERTAIN"

    # Authority consumes the failed capture and must never infer flat.
    assert mon._refresh_live_broker_authority(strategy) is None
    assert mon._live_broker_flat_proven is False
    assert mon._broker_authority_degraded is True

    channels = ("ENTRY", "ADD_POSITION", "REVERSAL", "REBUILD", "MTS_EXIT")
    for channel in channels:
        order = manager.create_order(
            "TMFI6", OrderSide.BUY, OrderType.MARKET, 1,
            strategy=channel)
        assert manager.submit(order) is False, channel
    assert sends == []

    key = {"trade_id": "t", "session_generation": "s", "contract": "TMFI6",
           "closing_side": "SELL", "qty": 1}
    assert mon._leg_lock_acquire(key) is True
    assert mon._leg_lock_check(key) is True
    assert mon._leg_lock_load()
    assert sends == []
