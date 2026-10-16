"""Live spread_z visibility: the telemetry state must carry the current
spread_z so the dashboard/user can see the live distance to the entry gate
(|z| >= entry_z, default 2.5 ATR).  Module-level telemetry function writes
telemetry-only fields; spread_z is a pure telemetry projection.
"""
import json
import os

from strategies.plugins.futures.active.tmf_spread import _write_mts_telemetry


def _tmp_state(tmp_path, monkeypatch):
    p = str(tmp_path / "mts_position_state.json")
    monkeypatch.setenv("MTS_POSITION_STATE_PATH", p)
    # _write_mts_telemetry resolves the state path via _get_state_file_path()
    from strategies.plugins.futures.active import tmf_spread as mod
    monkeypatch.setattr(mod, "_MTS_STATE_FILE", p)
    with open(p, "w") as f:
        json.dump({"has_position": False, "state": "HEARTBEAT",
                   "state_revision": 3, "schema_version": 3}, f)
    return p


def test_telemetry_writes_live_spread_z(tmp_path, monkeypatch):
    p = _tmp_state(tmp_path, monkeypatch)
    _write_mts_telemetry(near_last=45819.0, far_last=45955.0,
                         spread_z=1.75)
    d = json.load(open(p))
    assert d["spread_z"] == 1.75
    # telemetry-only: lifecycle fields untouched
    assert d["has_position"] is False
    assert d["state"] == "HEARTBEAT"


def test_telemetry_spread_z_absent_when_not_provided(tmp_path, monkeypatch):
    p = _tmp_state(tmp_path, monkeypatch)
    _write_mts_telemetry(near_last=45819.0, far_last=45955.0)
    d = json.load(open(p))
    assert d.get("spread_z", 0.0) == 0.0
