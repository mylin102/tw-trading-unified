"""The flat dashboard projection must not resurrect stale strategy legs."""

import json
from types import SimpleNamespace

import strategies.futures.monitor as monitor_module


def test_sync_mts_flat_ignores_stale_strategy_position_memory(tmp_path, monkeypatch):
    state_path = tmp_path / "mts_position_state.json"
    state_path.write_text(json.dumps({
        "has_position": False,
        "near_side": "SHORT", "far_side": "LONG",
        "near_entry": 47579.0, "far_entry": 47720.0,
        "released_leg": "near", "remaining_leg": "far",
        "near_last": 47580.0, "far_last": 47721.0,
    }))
    monkeypatch.setattr(monitor_module, "_mts_position_state_path", lambda: state_path)
    monkeypatch.setattr(monitor_module.FuturesMonitor, "initial_balance", property(lambda self: 0))
    strategy = SimpleNamespace(
        _has_position=False, _near_side="SHORT", _far_side="LONG",
        _near_entry=47579.0, _far_entry=47720.0,
        _released_leg="near", _remaining_leg="far",
    )
    monitor = monitor_module.FuturesMonitor.__new__(monitor_module.FuturesMonitor)
    monitor.cfg = {"mts": {"strategy": "tmf_spread"}}
    monitor._registry = {"tmf_spread": strategy}
    monitor.market_data = {}
    monitor._far_current_bar = {}
    monitor.ticker = "TMF"
    monitor._pending_lifecycle_orders = {}
    monitor._manual_trade_status = "READY"
    monitor.trader = SimpleNamespace(balance=0)
    monitor.live_trading = False
    monitor.api = None

    monitor._sync_mts_status()

    actual = json.loads(state_path.read_text())
    assert actual["has_position"] is False
    assert actual["reason"] == "mts_sync_status"
    assert actual["near_side"] is None
    assert actual["far_side"] is None
    assert actual["near_entry"] == 0.0
    assert actual["far_entry"] == 0.0
    assert actual.get("released_leg") is None
    assert actual.get("remaining_leg") is None
    assert actual["near_last"] == 47580.0
    assert actual["far_last"] == 47721.0
