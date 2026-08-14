"""Option B (bounded): BROKER_CANONICAL_RUNTIME fallback for the MTS
performance scope.  The fills ledger historically lacks per-record
provenance so the strict classifier degrades to legacy and hides real
broker UPL.  Strict fallback, LIVE only:

- is_live_runtime (live_ready) required
- CURRENT session's broker canonical snapshot only (session match)
- account_identity_hash + canonical_input_hash + session_id present
- both legs (TMFH6/TMFI6) carry direction/quantity/avg_cost/pnl
- fresh (captured_at < 60s) + capture OK
- legacy ledger is history only — never mixed into UPL
- missing/stale/session-mismatch -> N/A (ok=False)
- Paper mode completely unchanged
"""
import json
import time
from pathlib import Path

import core.runtime_paths as rp
from core.performance_provenance import scope_mts_performance


def _canonical(captured_at_ms: int, session_id: str = "sess-123",
               complete: bool = True) -> dict:
    d = {
        "source": "live_broker",
        "mode": "live",
        "scope": "canonical",
        "captured_at": captured_at_ms,
        "fetch_status": {"capture": "OK"},
        "account_identity_hash": "acct-hash-1",
        "canonical_input_hash": "input-hash-1",
        "session_id": session_id,
        "positions": [
            {"account": "futures", "code": "TMFH6", "direction": "Action.Sell",
             "quantity": 1, "avg_cost": 45962.0, "pnl": 650.0},
            {"account": "futures", "code": "TMFI6", "direction": "Action.Buy",
             "quantity": 1, "avg_cost": 46088.0, "pnl": -680.0},
        ],
        "open_orders": [],
    }
    if not complete:
        del d["positions"][0]["pnl"]
    return d


def _write_canonical(tmp_path, payload: dict) -> Path:
    p = tmp_path / "broker_snapshot_canonical.json"
    p.write_text(json.dumps(payload))
    return p


def _live_truth(session_id: str = "sess-123") -> dict:
    return {
        "is_live_runtime": True, "is_paper_runtime": False,
        "session_id": session_id, "config_hash": "cfg-1",
    }


def _legacy_evidence() -> dict:
    return {"evidence_mode": "legacy", "record_count": 392,
            "run_ids": [], "config_hashes": [], "sessions": [],
            "sources": [], "reason": "legacy ledger without provenance"}


def test_full_canonical_live_fallback_ok(tmp_path, monkeypatch):
    canon = _canonical(int(time.time() * 1000))
    _write_canonical(tmp_path, canon)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is True
    assert scope["provenance"] == "BROKER_CANONICAL_RUNTIME"
    assert scope["mode"] == "live"


def test_stale_canonical_na(tmp_path, monkeypatch):
    canon = _canonical(int(time.time() * 1000) - 120_000)  # >60s old
    _write_canonical(tmp_path, canon)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is False


def test_missing_leg_pnl_na(tmp_path, monkeypatch):
    canon = _canonical(int(time.time() * 1000), complete=False)
    _write_canonical(tmp_path, canon)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is False


def test_session_mismatch_na(tmp_path, monkeypatch):
    canon = _canonical(int(time.time() * 1000), session_id="other-session")
    _write_canonical(tmp_path, canon)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is False


def test_paper_runtime_unchanged(tmp_path, monkeypatch):
    canon = _canonical(int(time.time() * 1000))
    _write_canonical(tmp_path, canon)
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    paper = {"is_live_runtime": False, "is_paper_runtime": True,
             "session_id": "sess-123", "config_hash": "cfg-1"}
    scope = scope_mts_performance(paper, _legacy_evidence())
    # paper path untouched: legacy records ARE paper evidence (compat)
    assert scope["ok"] is True
    assert scope.get("provenance") != "BROKER_CANONICAL_RUNTIME"


def test_no_canonical_file_na(tmp_path, monkeypatch):
    monkeypatch.setattr(rp, "runtime_path",
                        lambda *parts: str(tmp_path / parts[-1]))
    scope = scope_mts_performance(_live_truth(), _legacy_evidence())
    assert scope["ok"] is False
