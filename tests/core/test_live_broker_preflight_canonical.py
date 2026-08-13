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

def test_safe_positions_preserves_direction_and_avg_cost():
    """Broker position identity (code/qty/direction/avg_cost/pnl) is
    preserved in the preflight evidence."""
    from core.live_broker_preflight import _safe_positions

    class _Pos:
        def __init__(self, code, quantity, price, direction, pnl):
            self.code = code
            self.quantity = quantity
            self.price = price
            self.direction = direction
            self.pnl = pnl

    class _Api:
        def list_positions(self, account):
            return [_Pos("TMFH6", 1, 46077.0, "Sell", 1850.0),
                    _Pos("TMFI6", 1, 45231.0, "Buy", -1740.0)]

    rows = _safe_positions(_Api(), object())
    by_code = {r["code"]: r for r in rows}
    assert by_code["TMFH6"]["qty"] == 1
    assert by_code["TMFH6"]["direction"] == "Sell"
    assert by_code["TMFH6"]["avg_cost"] == 46077.0
    assert by_code["TMFH6"]["pnl"] == 1850.0
    assert by_code["TMFI6"]["direction"] == "Buy"
    assert by_code["TMFI6"]["avg_cost"] == 45231.0


def _canonical_snap(tmp_path, name="snap.json", **over):
    """A fully-canonical live broker snapshot (the shape written by the
    preflight adapter)."""
    data = {
        "source": "live_broker",
        "mode": "live",
        "account_identity_hash": "acc-hash-123",
        "session_id": "req-42",
        "canonical_input_hash": "sha256-deadbeef",
        "captured_at": int(datetime.now(timezone.utc).timestamp() * 1000),
        "positions": [
            {"code": "TMFH6", "qty": 1, "direction": "Sell",
             "avg_cost": 46077.0, "pnl": 1850.0},
            {"code": "TMFI6", "qty": 1, "direction": "Buy",
             "avg_cost": 45231.0, "pnl": -1740.0},
        ],
        "open_orders": [],
    }
    data.update(over)
    snap = tmp_path / name
    snap.write_text(json.dumps(data), encoding="utf-8")
    return snap


def test_broker_snapshot_live_upl_fresh_canonical(tmp_path):
    """Live UPL requires the full canonical live-broker provenance and
    the exact expected MTS legs with direction/qty/avg_cost."""
    from core.performance_provenance import broker_snapshot_live_upl

    snap = _canonical_snap(tmp_path)
    upl, reason = broker_snapshot_live_upl(snap, session_id="req-42")
    assert reason is None, reason
    assert upl == {"TMFH6": 1850.0, "TMFI6": -1740.0}


def test_broker_snapshot_live_upl_missing_stale(tmp_path):
    """No/stale snapshot => None + a reason (N/A, never fabricated)."""
    from core.performance_provenance import broker_snapshot_live_upl

    upl, reason = broker_snapshot_live_upl(tmp_path / "none.json")
    assert upl is None and reason

    stale = _canonical_snap(tmp_path, name="stale.json",
                            captured_at=1754000000000)  # 2025-08
    upl2, reason2 = broker_snapshot_live_upl(stale, session_id="req-42")
    assert upl2 is None and reason2


def test_broker_snapshot_live_upl_requires_provenance(tmp_path):
    """Missing/mismatched canonical provenance => N/A (fail-closed)."""
    from core.performance_provenance import broker_snapshot_live_upl

    for over, tag in (
        ({"source": "paper"}, "source"),
        ({"mode": "paper"}, "mode"),
        ({"account_identity_hash": ""}, "account"),
        ({"canonical_input_hash": ""}, "hash"),
        ({"session_id": ""}, "session"),
    ):
        snap = _canonical_snap(tmp_path, name=tag + ".json", **over)
        upl, reason = broker_snapshot_live_upl(snap, session_id="req-42")
        assert upl is None and reason, (tag, reason)


def test_broker_snapshot_live_upl_session_mismatch(tmp_path):
    """A snapshot taken under a different session than the current
    runtime context => N/A (fail-closed)."""
    from core.performance_provenance import broker_snapshot_live_upl

    snap = _canonical_snap(tmp_path, name="sess.json")
    upl, reason = broker_snapshot_live_upl(snap, session_id="req-999")
    assert upl is None and "session" in reason


def test_broker_snapshot_live_upl_requires_exact_legs(tmp_path):
    """Expected TMFH6/TMFI6 legs must carry direction/qty/avg_cost;
    missing identity or extra/missing legs => N/A."""
    from core.performance_provenance import broker_snapshot_live_upl

    snap = _canonical_snap(tmp_path, name="legs.json",
                           positions=[
                               {"code": "TMFH6", "qty": 1, "pnl": 1850.0},
                               {"code": "TMFI6", "qty": 1, "direction": "Buy",
                                "avg_cost": 45231.0, "pnl": -1740.0},
                           ])
    upl, reason = broker_snapshot_live_upl(snap, session_id="req-42")
    assert upl is None and reason  # near leg lacks direction/avg_cost

    snap2 = _canonical_snap(tmp_path, name="extra.json",
                            positions=[
                                {"code": "TMFH6", "qty": 1,
                                 "direction": "Sell", "avg_cost": 46077.0,
                                 "pnl": 1850.0},
                            ])
    upl2, reason2 = broker_snapshot_live_upl(snap2, session_id="req-42")
    assert upl2 is None and reason2  # far leg missing

