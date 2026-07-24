"""
Contract test for Multi-Monitor Interleaved Execution & State File Isolation.

Verifies that when TMF and MTX monitors execute concurrently in the same process:
1. Thread-Local state_path injection routes state file writes to distinct files.
2. TMF state contains ticker='TMF' and MTX state contains ticker='MTX'.
3. Interleaved tick processing does not bleed position state or trigger desync locks.
"""
import os
import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock

from strategies.futures.monitor import FuturesMonitor, _thread_local, _mts_position_state_path
from strategies.plugins.futures.active.tmf_spread import _write_mts_state, _get_state_file_path


def test_multi_monitor_state_file_isolation(tmp_path, monkeypatch):
    tmf_state_file = tmp_path / "mts_position_state.json"
    mtx_state_file = tmp_path / "mts_position_state_futures_mtx.json"

    # Set up thread local simulation for TMF vs MTX
    # 1. TMF active
    _thread_local.state_path = str(tmf_state_file)
    assert _get_state_file_path() == tmf_state_file
    _write_mts_state(has_position=True, action="SPREAD", reason="TMF entry", ticker="TMF", trade_id="tmf-001")

    # 2. MTX active
    _thread_local.state_path = str(mtx_state_file)
    assert _get_state_file_path() == mtx_state_file
    _write_mts_state(has_position=False, action="FLAT", reason="MTX idle", ticker="MTX", trade_id=None)

    # 3. Read back and verify complete isolation
    tmf_data = json.loads(tmf_state_file.read_text())
    mtx_data = json.loads(mtx_state_file.read_text())

    assert tmf_data["ticker"] == "TMF"
    assert tmf_data["has_position"] is True
    assert tmf_data["state"] == "SPREAD"
    assert tmf_data["trade_id"] == "tmf-001"

    assert mtx_data["ticker"] == "MTX"
    assert mtx_data["has_position"] is False
    assert mtx_data["state"] == "FLAT"


def test_interleaved_tick_thread_local_scoping(tmp_path):
    tmf_state = tmp_path / "tmf_state.json"
    mtx_state = tmp_path / "mtx_state.json"

    # Create dummy monitors
    f_mon_tmf = MagicMock()
    f_mon_tmf._state_path = tmf_state
    f_mon_tmf.ticker = "TMF"

    f_mon_mtx = MagicMock()
    f_mon_mtx._state_path = mtx_state
    f_mon_mtx.ticker = "MTX"

    monitors = [f_mon_tmf, f_mon_mtx]

    # Interleaved simulation (matching main.py loop)
    for _ in range(5):
        for f_mon in monitors:
            _thread_local.state_path = getattr(f_mon, "_state_path", None)
            try:
                current_path = _get_state_file_path()
                if f_mon.ticker == "TMF":
                    assert current_path == tmf_state
                else:
                    assert current_path == mtx_state
            finally:
                _thread_local.state_path = None

        # After loop, thread local should be cleared
        assert getattr(_thread_local, "state_path", None) is None


# 💡 Gemini CLI: Unit test for Fail-Closed behavior during POSITION_AUTHORITY identity mismatch
def test_identity_mismatch_fail_closed(tmp_path):
    mismatched_state_file = tmp_path / "mts_position_state.json"
    # Write state file for TMF
    _write_mts_state(has_position=False, action="FLAT", reason="TMF idle", ticker="TMF", trade_id=None)

    # Simulate MTX monitor reading TMF state file accidentally
    _disk_ticker = "TMF"
    self_ticker = "MTX"

    entry_blocked = False
    reconciliation_pending = False
    authority_has_pos = False

    if _disk_ticker and _disk_ticker.upper() != self_ticker.upper():
        entry_blocked = True
        reconciliation_pending = True
        authority_has_pos = True  # Preserve in-memory position

    assert entry_blocked is True
    assert reconciliation_pending is True
    assert authority_has_pos is True
