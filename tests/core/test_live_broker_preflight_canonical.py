"""Canonical gate-artifact adapter tests.

The same read-only preflight response derives a FLAT canonical gate
artifact (source=live_broker / mode=live / positions / open_orders /
account_identity_hash / scope=futopt / captured_at epoch-ms int /
available_margin / canonical_input_hash from the sorted JSON of the
account hash + futures positions + open_orders + available_margin +
captured_at).  No manual values; the paper path and the gate contract
stay untouched.
"""
import hashlib
import json
import time
from datetime import datetime, timezone

import pytest


def _response(**over):
    return {
        "schema_version": 1,
        "request_id": "LIVE-PREFLIGHT-test",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "read_only": True,
        "live_order_allowed": False,
        "snapshot": {
            "account_id_hash": "a" * 64,
            "position_snapshot_time": datetime.now(timezone.utc).isoformat(),
            "order_snapshot_time": datetime.now(timezone.utc).isoformat(),
            "positions": [],
            "open_orders": [],
            "margin": {"available_margin": 300_000.0,
                       "equity_amount": 1_000_000.0, "risk_indicator": 1},
            "trading_limits": None,
            "contracts": {"near": {"code": "TMFH6"},
                          "far": {"code": "TMFI6"}},
            "snapshot_codes": ["TMFH6", "TMFI6"],
            "quote_subscription": {"near": True, "far": True},
            "query_failures": [],
            "warnings": [],
        },
        "preflight": {"passed": True, "failed_checks": []},
        **over,
    }


def test_canonical_artifact_flat_fields_derived_from_response():
    """Every flat field is derived from the SAME response snapshot."""
    from core.live_broker_preflight import derive_canonical_gate_artifact

    art = derive_canonical_gate_artifact(_response())
    assert art["source"] == "live_broker"
    assert art["mode"] == "live"
    assert art["positions"] == []
    assert art["open_orders"] == []
    assert art["account_identity_hash"] == "a" * 64
    assert art["scope"] == "futopt"
    assert art["available_margin"] == 300_000.0
    assert isinstance(art["canonical_input_hash"], str)
    assert len(art["canonical_input_hash"]) == 64
    # the derived capture identifier is the same request
    assert art["session_id"] == "LIVE-PREFLIGHT-test"


def test_canonical_artifact_captured_at_epoch_ms_int():
    """captured_at is the canonical epoch-ms INTEGER of the response's
    captured_at — never a string/float/time.time() artifact."""
    from core.live_broker_preflight import derive_canonical_gate_artifact

    art = derive_canonical_gate_artifact(_response())
    assert isinstance(art["captured_at"], int)
    assert not isinstance(art["captured_at"], bool)
    assert art["captured_at"] >= 1_000_000_000_000  # epoch-ms range
    _expected = int(
        datetime.fromisoformat(_response()["captured_at"]).timestamp() * 1000)
    assert abs(art["captured_at"] - _expected) < 5_000


def test_canonical_input_hash_content_addressed():
    """The hash is deterministic, change-sensitive and exactly the
    sha256 of the sorted JSON of the five canonical inputs."""
    from core.live_broker_preflight import derive_canonical_gate_artifact

    r1 = _response()
    art1 = derive_canonical_gate_artifact(r1)
    art2 = derive_canonical_gate_artifact(_response())
    assert art1["canonical_input_hash"] == art2["canonical_input_hash"]

    _snap = r1["snapshot"]
    _cap_ms = art1["captured_at"]
    _expected = hashlib.sha256(json.dumps({
        "account_identity_hash": _snap["account_id_hash"],
        "positions": _snap["positions"],
        "open_orders": _snap["open_orders"],
        "available_margin": _snap["margin"]["available_margin"],
        "captured_at": _cap_ms,
    }, sort_keys=True, ensure_ascii=False, default=str).encode(
        "utf-8")).hexdigest()
    assert art1["canonical_input_hash"] == _expected

    # a changed position changes the digest (content-addressed)
    _changed = _response()
    _changed["snapshot"] = dict(_snap, positions=[{
        "code": "TMFH6", "quantity": 1, "account": "futures"}])
    art3 = derive_canonical_gate_artifact(_changed)
    assert art3["canonical_input_hash"] != art1["canonical_input_hash"]


def test_canonical_artifact_passes_gate_guards(tmp_path):
    """The artifact satisfies the deployment gate's flat/margin/capture
    guards (same file = same capture)."""
    from core.live_broker_preflight import derive_canonical_gate_artifact
    from core.deployment_safety_gate import (
        guard_flat_no_pending, guard_margin, guard_capture_consistency)

    artifact = derive_canonical_gate_artifact(_response())
    pf = tmp_path / "position_state.json"
    pf.write_text(json.dumps(artifact), encoding="utf-8")
    ctx = {"effective_mode": "live",
           "session_id": artifact["session_id"]}

    flat = guard_flat_no_pending(str(pf), ctx)
    assert flat.ok, flat.reasons
    margin = guard_margin(artifact["available_margin"],
                          margin_evidence=artifact)
    assert margin.ok, margin.reasons
    capture = guard_capture_consistency(str(pf), artifact)
    assert capture.ok, capture.reasons


def test_paper_path_preserved(tmp_path):
    """The paper deployment path is unchanged (paper snapshot for a
    paper context still passes; it never claims live)."""
    from core.deployment_safety_gate import guard_flat_no_pending

    pf = tmp_path / "paper.json"
    pf.write_text(json.dumps({
        "source": "paper", "mode": "paper",
        "positions": [{"code": "TMFH6", "quantity": 1}],
        "open_orders": [],
        "captured_at": int(time.time() * 1000),
        "canonical_input_hash": "c" * 64,
    }), encoding="utf-8")
    r = guard_flat_no_pending(str(pf), {"effective_mode": "paper"})
    assert r.ok, r.reasons
    r2 = guard_flat_no_pending(str(pf), {"effective_mode": "live"})
    assert not r2.ok
    assert "GUARD_SNAPSHOT_PAPER_NOT_LIVE" in r2.reasons
