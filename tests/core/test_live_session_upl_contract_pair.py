"""RED/GREEN: the live session UPL artifact must validate for any pair.

2026-09-24: the artifact carried TMFJ6/TMFK6 (fresh, session-matched, both
legs with pnl) while the validator demanded the literal pair
{"TMFH6", "TMFI6"} -> a valid UPL artifact was rejected -> the dashboard
showed N/A for a real broker position.
"""
import json
import time

import core.runtime_paths as rp
from core.performance_provenance import _read_live_session_upl


def _artifact(legs, session_id="sess-123"):
    return {
        "source": "live_broker_session",
        "session_id": session_id,
        "captured_at": int(time.time() * 1000),
        "legs": legs,
        "total_pnl": 40.0,
    }


def _leg(direction, qty, avg_cost, pnl):
    return {"direction": direction, "quantity": qty,
            "avg_cost": avg_cost, "pnl": pnl}


def _read(tmp_path, monkeypatch, payload):
    (tmp_path / "live_session_upl.json").write_text(
        json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    return _read_live_session_upl(
        {"is_live_runtime": True, "session_id": "sess-123"})


def test_rolled_pair_artifact_is_accepted(tmp_path, monkeypatch):
    out = _read(tmp_path, monkeypatch, _artifact({
        "TMFJ6": _leg("Action.Sell", 1, 48176.0, 2510.0),
        "TMFK6": _leg("Action.Buy", 1, 48320.0, -2470.0),
    }))
    assert out is not None
    assert sorted(out["legs"]) == ["TMFJ6", "TMFK6"]


def test_single_leg_artifact_is_still_rejected(tmp_path, monkeypatch):
    out = _read(tmp_path, monkeypatch, _artifact({
        "TMFK6": _leg("Action.Buy", 1, 48320.0, -2470.0),
    }))
    assert out is None


def test_missing_leg_pnl_is_still_rejected(tmp_path, monkeypatch):
    out = _read(tmp_path, monkeypatch, _artifact({
        "TMFJ6": _leg("Action.Sell", 1, 48176.0, None),
        "TMFK6": _leg("Action.Buy", 1, 48320.0, -2470.0),
    }))
    assert out is None
