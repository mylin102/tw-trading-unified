"""Broker-flat MTS state must carry the telemetry spread_z/atr so the
dashboard renders live values instead of N/A.  The LIVE broker-first design
never falls through to the /tmp legacy state file for lifecycle truth, but
the flat stub it builds omits telemetry-only fields (spread_z, atr) that
the display needs — both rendered N/A even though /tmp state carries them.
"""
import json
from pathlib import Path

from ui.dashboard import _build_flat_broker_mts_state


def test_flat_state_carries_telemetry_spread_z_and_atr():
    st = _build_flat_broker_mts_state({"spread_z": 15.59, "atr": 62.17})
    assert st["has_position"] is False
    assert st["reason"] == "broker_snapshot_flat"
    assert st["spread_z"] == 15.59
    assert st["atr"] == 62.17


def test_flat_state_tolerates_missing_telemetry():
    st = _build_flat_broker_mts_state({})
    assert st["has_position"] is False
    assert st.get("spread_z") is None
    assert st.get("atr") is None
