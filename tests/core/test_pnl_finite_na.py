"""Round-3 review fixes: a missing or non-finite broker pnl must render
N/A — never 0.  The monitor writer must preserve None (not coerce to 0),
and the artifact reader must reject None / NaN / Inf / non-numeric pnl.
"""
import json
import math
import time
from types import SimpleNamespace

import core.runtime_paths as rp
from core.performance_provenance import scope_mts_performance
from tests.core.test_fills_recovery_live_gate import _AutoMonitor


def _artifact(captured_at_ms=None, session_id="sess-123", pnl=None):
    d = {
        "source": "live_broker_session",
        "session_id": session_id,
        "captured_at": captured_at_ms or int(time.time() * 1000),
        "legs": {
            "TMFH6": {"direction": "S", "quantity": 1, "avg_cost": 45962.0,
                      "pnl": 650.0},
            "TMFI6": {"direction": "B", "quantity": 1, "avg_cost": 46088.0,
                      "pnl": -680.0},
        },
        "total_pnl": -30.0,
    }
    if pnl is not None:
        d["legs"]["TMFH6"]["pnl"] = pnl
    return d


def _write_artifact(tmp_path, payload):
    p = tmp_path / "live_session_upl.json"
    p.write_text(json.dumps(payload))
    return p


def _live_truth():
    return {"is_live_runtime": True, "is_paper_runtime": False,
            "session_id": "sess-123", "config_hash": "cfg-1"}


def _legacy_evidence():
    return {"evidence_mode": "legacy", "record_count": 392,
            "run_ids": [], "config_hashes": [], "sessions": [],
            "sources": [], "reason": "legacy ledger without provenance"}


def _scope_for(tmp_path, monkeypatch, payload):
    _write_artifact(tmp_path, payload)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    return scope_mts_performance(_live_truth(), _legacy_evidence())


def test_reader_na_none_pnl(tmp_path, monkeypatch):
    d = _artifact()
    d["legs"]["TMFH6"]["pnl"] = None  # explicit null in the artifact
    assert _scope_for(tmp_path, monkeypatch, d)["ok"] is False


def test_reader_na_nan_pnl(tmp_path, monkeypatch):
    d = _artifact(pnl=float("nan"))
    assert _scope_for(tmp_path, monkeypatch, d)["ok"] is False


def test_reader_na_inf_pnl(tmp_path, monkeypatch):
    d = _artifact(pnl=float("inf"))
    assert _scope_for(tmp_path, monkeypatch, d)["ok"] is False


def test_reader_na_non_numeric_pnl(tmp_path, monkeypatch):
    d = _artifact(pnl="not-a-number")
    assert _scope_for(tmp_path, monkeypatch, d)["ok"] is False


def test_writer_preserves_none_pnl(tmp_path, monkeypatch):
    mon = _AutoMonitor.__new__(_AutoMonitor)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    ctx = SimpleNamespace(session_id="sess-1")
    mon._write_live_session_upl(
        [{"code": "TMFH6", "direction": "S", "quantity": 1,
          "avg_cost": 45962.0, "pnl": None}], ctx)
    d = json.loads((tmp_path / "live_session_upl.json").read_text())
    assert d["legs"]["TMFH6"]["pnl"] is None  # never coerced to 0
