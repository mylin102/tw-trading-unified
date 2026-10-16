"""Option B v3 (review round 2): the trading-system's EXISTING Shioaji
session executes the read-only list_positions().pnl and writes a live UPL
artifact; the dashboard only READS that artifact — it never opens a second
Shioaji session and never falls back to local state UPL.  Query failure /
missing legs / stale / session mismatch / no artifact -> N/A.  Paper
unchanged.  Provenance BROKER_CANONICAL_RUNTIME.
"""
import json
import time

import core.runtime_paths as rp
from core.performance_provenance import scope_mts_performance


def _artifact(captured_at_ms=None, session_id="sess-123", complete=True,
              missing_file=False):
    if missing_file:
        return None
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
    if not complete:
        del d["legs"]["TMFH6"]["pnl"]
    return d


def _write_artifact(tmp_path, payload):
    p = tmp_path / "live_session_upl.json"
    p.write_text(json.dumps(payload))
    return p


def _live_truth(session_id="sess-123"):
    return {"is_live_runtime": True, "is_paper_runtime": False,
            "session_id": session_id, "config_hash": "cfg-1"}


def _legacy_evidence():
    return {"evidence_mode": "legacy", "record_count": 392,
            "run_ids": [], "config_hashes": [], "sessions": [],
            "sources": [], "reason": "legacy ledger without provenance"}


def test_scope_ok_with_fresh_artifact(tmp_path, monkeypatch):
    _write_artifact(tmp_path, _artifact())
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is True
    assert scope["provenance"] == "BROKER_CANONICAL_RUNTIME"
    assert scope["mode"] == "live"


def test_scope_na_stale_artifact(tmp_path, monkeypatch):
    _write_artifact(tmp_path, _artifact(int(time.time() * 1000) - 120_000))
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is False


def test_scope_na_missing_leg_pnl(tmp_path, monkeypatch):
    _write_artifact(tmp_path, _artifact(complete=False))
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is False


def test_scope_na_session_mismatch(tmp_path, monkeypatch):
    _write_artifact(tmp_path, _artifact(session_id="other-session"))
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is False


def test_scope_na_no_artifact(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is False  # no local state fallback


def test_paper_unchanged(tmp_path, monkeypatch):
    _write_artifact(tmp_path, _artifact())
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    paper = {"is_live_runtime": False, "is_paper_runtime": True,
             "session_id": "sess-123", "config_hash": "cfg-1"}
    scope = scope_mts_performance(paper, _legacy_evidence())
    assert scope["ok"] is True  # paper + legacy compat untouched
    assert scope.get("provenance") != "BROKER_CANONICAL_RUNTIME"
