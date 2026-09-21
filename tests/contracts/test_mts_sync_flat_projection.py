"""The flat dashboard projection must not resurrect stale strategy legs."""

import json
from types import SimpleNamespace

import strategies.futures.monitor as monitor_module
import strategies.plugins.futures.active.tmf_spread as tmf_spread_module


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
    remembered = actual["last_known_legs"]
    assert remembered == {
        "near_side": "SHORT", "far_side": "LONG",
        "near_entry": 47579.0, "far_entry": 47720.0,
        "released_leg": "near", "remaining_leg": "far",
        "updated_at": remembered["updated_at"],
        "source": "strategy_memory",
    }
    assert remembered["updated_at"]


def test_broker_positive_local_flat_recovery_reads_last_known_legs(tmp_path, monkeypatch):
    state_path = tmp_path / "mts_position_state.json"
    state_path.write_text(json.dumps({
        "has_position": False,
        "near_side": None, "far_side": None,
        "near_entry": 0.0, "far_entry": 0.0,
        "last_known_legs": {
            "near_side": "STALE_MUST_NOT_BE_USED",
            "far_side": "LONG",
            "near_entry": 47579.0,
            "far_entry": 47720.0,
            "released_leg": "near",
            "remaining_leg": "far",
            "updated_at": "2026-09-21T10:00:00+08:00",
            "source": "strategy_memory",
        },
    }))
    monkeypatch.setattr(monitor_module, "_mts_position_state_path", lambda: state_path)
    strategy = SimpleNamespace(
        _near_entry=0.0, _far_entry=0.0,
        _near_side=None, _far_side=None, _released_leg=None,
    )
    monitor = monitor_module.FuturesMonitor.__new__(monitor_module.FuturesMonitor)

    monitor._restore_mts_recovery_legs_from_last_known(strategy, broker_position=-1)

    assert strategy._near_entry == 47579.0
    assert strategy._far_entry == 47720.0
    assert strategy._near_side == "SHORT"  # broker position is authoritative
    assert strategy._far_side == "LONG"
    assert strategy._released_leg == "near"


def test_telemetry_preserves_last_known_legs_verbatim(tmp_path, monkeypatch):
    state_path = tmp_path / "mts_position_state.json"
    remembered = {
        "near_side": "SHORT", "far_side": "LONG",
        "near_entry": 47579.0, "far_entry": 47720.0,
        "released_leg": "near", "remaining_leg": "far",
        "updated_at": "2026-09-21T10:00:00+08:00",
        "source": "strategy_memory",
    }
    state_path.write_text(json.dumps({
        "has_position": False, "state": "HEARTBEAT",
        "last_known_legs": remembered,
    }))
    monkeypatch.setattr(tmf_spread_module, "_get_state_file_path", lambda: state_path)
    monkeypatch.setattr(tmf_spread_module, "_PENDING_STATE_REWRITE", None)

    tmf_spread_module._write_mts_telemetry(
        near_last=47580.0, far_last=47721.0, spread_z=1.25,
    )

    assert json.loads(state_path.read_text())["last_known_legs"] == remembered


def test_real_write_order_preserves_recoverable_last_known_legs(tmp_path, monkeypatch):
    state_path = tmp_path / "mts_position_state.json"
    monkeypatch.setattr(monitor_module, "_mts_position_state_path", lambda: state_path)
    monkeypatch.setattr(tmf_spread_module, "_get_state_file_path", lambda: state_path)
    monkeypatch.setattr(tmf_spread_module, "_PENDING_STATE_REWRITE", None)
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
    tmf_spread_module._write_mts_telemetry(near_last=47580.0, far_last=47721.0)
    strategy._near_entry = strategy._far_entry = 0.0
    strategy._near_side = strategy._far_side = strategy._released_leg = None
    monitor._restore_mts_recovery_legs_from_last_known(strategy, broker_position=-1)

    assert strategy._near_entry == 47579.0
    assert strategy._far_entry == 47720.0
    assert strategy._near_side == "SHORT"
    assert strategy._far_side == "LONG"
    assert strategy._released_leg == "near"
