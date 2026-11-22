"""2026-11-22 Gemini CLI: Regression tests ensuring all direct mts_position_state.json
write paths stamp runtime identity metadata so validate_runtime_state() returns (True, "OK").
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core import runtime_paths as rp
from strategies.futures.monitor import FuturesMonitor
from strategies.plugins.futures.active import tmf_spread


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    """Isolate TRADING_RUNTIME_DIR and MTS state path into a temp directory."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TRADING_RUNTIME_ID", raising=False)
    monkeypatch.delenv("MTS_STATE_PATH", raising=False)
    monkeypatch.delenv("MTS_BACKTEST", raising=False)
    rp.bind_mts_data_mode("paper")
    state_path = tmp_path / "paper" / "data" / "state" / "mts_position_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    # Ensure monitor.py's _mts_position_state_path() resolves to tmp_path even if
    # module-level MTS_POSITION_STATE_PATH was cached earlier in the test session.
    import strategies.futures.monitor as mon_mod

    monkeypatch.setattr(mon_mod, "_mts_position_state_path", lambda: state_path)
    monkeypatch.setattr(tmf_spread, "_get_state_file_path", lambda: str(state_path))
    return state_path


def test_sync_mts_status_heartbeat_stamps_identity_and_increments_generation(isolated_runtime):
    """_sync_mts_status() must stamp runtime identity and bump generation instead of stripping it."""
    initial = rp.stamp_runtime_state(
        {"has_position": False, "state": "FLAT", "near_last": 22000.0, "far_last": 22020.0},
        {"generation": 4},
    )
    isolated_runtime.write_text(json.dumps(initial), encoding="utf-8")

    # Minimal monitor stub exercising the real _sync_mts_status() method.
    mon = object.__new__(FuturesMonitor)
    mon.ticker = "TMF"
    mon.cfg = {"mts": {"strategy": "tmf_spread"}}
    strategy_stub = SimpleNamespace(
        _has_position=False,
        _near_side=None,
        _far_side=None,
        _near_entry=0.0,
        _far_entry=0.0,
        _released_leg=None,
        _remaining_leg=None,
    )
    mon._registry = {"tmf_spread": strategy_stub}
    mon._last_processed_data = {}
    mon.market_data = {"TMF": {"close": 22100.0}, "TMF_FAR": {"close": 22115.0}}
    mon._far_current_bar = {"close": 22115.0}
    mon._pending_lifecycle_orders = {}
    mon._manual_trade_status = "IDLE"
    mon.EXEC = {"initial_balance": 100000}
    mon.trader = SimpleNamespace(balance=100000)
    mon.live_trading = False
    mon.api = None

    mon._sync_mts_status()

    written = json.loads(isolated_runtime.read_text(encoding="utf-8"))
    ok, reason = rp.validate_runtime_state(written)
    assert (ok, reason) == (True, "OK")
    assert written["state"] == "HEARTBEAT"
    assert written["generation"] == 6


def test_clear_unconfirmed_combo_state_stamps_identity(isolated_runtime):
    """_clear_unconfirmed_combo_state() must stamp runtime identity on both fresh and existing files."""
    mon = object.__new__(FuturesMonitor)
    mon._append_mts_event = lambda *a, **kw: None

    # Case 1: state file does not exist yet
    mon._clear_unconfirmed_combo_state("mts-test-1", "UNCONFIRMED_TIMEOUT")
    written1 = json.loads(isolated_runtime.read_text(encoding="utf-8"))
    assert rp.validate_runtime_state(written1) == (True, "OK")
    assert written1["has_position"] is False
    assert written1["generation"] == 1

    # Case 2: existing stamped state file -> generation increments
    mon._clear_unconfirmed_combo_state("mts-test-2", "UNCONFIRMED_REJECTED")
    written2 = json.loads(isolated_runtime.read_text(encoding="utf-8"))
    assert rp.validate_runtime_state(written2) == (True, "OK")
    assert written2["generation"] == 2


def test_settle_mts_trade_stamps_identity(isolated_runtime):
    """settle_mts_trade() state update must stamp runtime identity even if disk state lacked it."""
    # Seed a state file missing identity fields (e.g. legacy writer) with generation=7
    isolated_runtime.write_text(
        json.dumps({"has_position": False, "state": "FLAT", "generation": 7}),
        encoding="utf-8",
    )

    tmf_spread.settle_mts_trade(
        ticker="TMF",
        trade_id="mts-settle-id-test",
        exit_type="NORMAL_EXIT",
        near_entry=22000.0,
        far_entry=22020.0,
        near_side="LONG",
        far_side="SHORT",
        near_exit_price=22100.0,
        far_exit_price=22050.0,
        settlement_source="TEST",
    )

    written = json.loads(isolated_runtime.read_text(encoding="utf-8"))
    assert rp.validate_runtime_state(written) == (True, "OK")
    assert written["generation"] == 8
    assert written["settlement_status"] == "SETTLED"


def test_pending_state_rewrite_replay_stamps_identity(isolated_runtime, monkeypatch):
    """_write_mts_telemetry() replay of _PENDING_STATE_REWRITE must produce valid runtime identity."""
    disk_state = rp.stamp_runtime_state({"has_position": True, "state_revision": 2}, {"generation": 3})
    isolated_runtime.write_text(json.dumps(disk_state), encoding="utf-8")

    # Queue an unstamped snapshot to verify replay stamps it before writing.
    monkeypatch.setattr(
        tmf_spread,
        "_PENDING_STATE_REWRITE",
        {"has_position": False, "state": "FLAT", "reason": "REPLAY_TEST"},
    )

    tmf_spread._write_mts_telemetry(near_last=22050.0, far_last=22070.0)

    written = json.loads(isolated_runtime.read_text(encoding="utf-8"))
    assert rp.validate_runtime_state(written) == (True, "OK")
    assert written["generation"] >= 5
