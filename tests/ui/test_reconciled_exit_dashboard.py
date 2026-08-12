"""Dashboard-only presentation contract for RECONCILED_EXIT_ONLY.

The restricted recovery mode must never reuse paper/local MTS state. Its
UPL is presentable only from the broker-attested capability and a current,
hash-bound dual-leg Shioaji BBO payload.
"""

from __future__ import annotations

import hashlib
import json

from ui.reconciled_exit_presentation import exit_only_upl_presentation


NOW_MS = 1_786_000_000_000


def _context():
    return {
        "requested_mode": "live",
        "effective_mode": "reconciled_exit_only",
        "live_order_allowed": False,
        "account_id_hash": "account-hash",
        "session_id": "session-1",
        "config_hash": "config-hash",
        "exit_only_capability": {
            "schema_version": 2,
            "reconciliation_id": "reconcile-1",
            "trade_id": "trade-1",
            "snapshot_hash": "snapshot-1",
            "snapshot_captured_at": NOW_MS - 1_000,
            "account_id_hash": "account-hash",
            "session_id": "session-1",
            "config_hash": "config-hash",
            "release_sha": "a" * 40,
            "legs": [
                {"symbol": "TMFH6", "side": "sell", "remaining_qty": 2,
                 "avg_cost": 100.0},
                {"symbol": "TMFI6", "side": "buy", "remaining_qty": 2,
                 "avg_cost": 90.0},
            ],
        },
    }


def _evidence(context, *, near_ts=NOW_MS - 1_000, far_ts=NOW_MS - 800):
    cap = context["exit_only_capability"]
    payload = {
        "version": 2,
        "near": {
            "symbol": "TMFH6", "bid": 99.0, "ask": 101.0,
            "exchange_ts": near_ts, "source": "shioaji_bidask",
        },
        "far": {
            "symbol": "TMFI6", "bid": 94.0, "ask": 96.0,
            "exchange_ts": far_ts, "source": "shioaji_bidask",
        },
        "reconciliation_id": cap["reconciliation_id"],
        "snapshot_hash": cap["snapshot_hash"],
        "config_hash": cap["config_hash"],
        "release_sha": cap["release_sha"],
        "session_id": cap["session_id"],
    }
    return {
        "bbo_hash": hashlib.sha256(json.dumps(
            payload, sort_keys=True, separators=(",", ":"),
            allow_nan=False).encode()).hexdigest(),
        "bbo_captured_at": max(near_ts, far_ts),
        "bbo_payload": payload,
    }


def test_exit_only_runtime_truth_not_unknown_quarantined():
    """[Dashboard] RECONCILED_EXIT_ONLY must not render
    UNKNOWN / QUARANTINED: classified as the limited-exit runtime
    (live profile, live_order_allowed False); LIVE/PAPER unchanged."""
    from ui.dashboard import summarize_execution_context

    ctx = {
        "requested_mode": "live",
        "effective_mode": "reconciled_exit_only",
        "live_order_allowed": False,
        "config_hash": "cfg-live",
    }
    truth = summarize_execution_context(
        ctx, {"futures_live.yaml": "cfg-live"})
    assert truth["is_exit_only_runtime"] is True
    assert truth["runtime_status"] == "RECONCILED_EXIT_ONLY"
    assert "UNKNOWN" not in truth["runtime_status"]
    assert "QUARANTINED" not in truth["runtime_status"]
    assert truth["warning"] == ""

    live = summarize_execution_context(
        {"requested_mode": "live", "effective_mode": "live_ready",
         "live_order_allowed": True, "config_hash": "cfg-live"},
        {"futures_live.yaml": "cfg-live"})
    assert live["runtime_status"] == "LIVE_READY"
    assert live["is_exit_only_runtime"] is False

    paper = summarize_execution_context(
        {"requested_mode": "paper", "effective_mode": "paper_active",
         "live_order_allowed": False, "config_hash": "cfg-paper"},
        {"futures.yaml (Paper baseline)": "cfg-paper"})
    assert paper["runtime_status"] == "PAPER_ACTIVE"
    assert paper["is_exit_only_runtime"] is False


def test_latest_bbo_evidence_from_events(tmp_path):
    """[Dashboard] the newest event carrying bbo_hash+bbo_payload is the
    evidence source; events without a payload are skipped; missing file
    => None."""
    from ui.reconciled_exit_presentation import (
        latest_bbo_evidence_from_events)

    p = tmp_path / "events.jsonl"
    p.write_text(
        json.dumps({"event": "OTHER"}) + "\n"
        + json.dumps({"bbo_hash": "h1",
                      "bbo_payload": {"version": 2, "near": {}}}) + "\n"
        + json.dumps({"bbo_hash": "h2",
                      "bbo_payload": {"version": 2, "near": {"x": 1}}})
        + "\n")
    ev = latest_bbo_evidence_from_events(str(p))
    assert ev is not None and ev["bbo_hash"] == "h2"
    assert latest_bbo_evidence_from_events(
        str(tmp_path / "missing.jsonl")) is None


def test_exit_only_valid_broker_attested_dual_bbo_calculates_pnl():
    context = _context()

    result = exit_only_upl_presentation(
        context, _evidence(context), now_ms=NOW_MS, point_value=10.0)

    # Short NEAR marks at ask: (100 - 101) * 2 * 10 = -20.
    # Long FAR marks at bid: (94 - 90) * 2 * 10 = +80.
    assert result["kind"] == "COMPUTED"
    assert result["near"]["pnl"] == -20.0
    assert result["far"]["pnl"] == 80.0
    assert result["total_pnl"] == 60.0
    assert result["source"] == "broker_attested_dual_bbo"


def test_exit_only_legacy_paper_data_is_never_a_pnl_fallback():
    context = _context()
    legacy_paper_state = {"has_position": True, "near_upl": 999999,
                          "far_upl": 999999}

    result = exit_only_upl_presentation(
        context, None, now_ms=NOW_MS, legacy_state=legacy_paper_state)

    assert result["kind"] == "NA"
    assert result["reason"] == "EXIT_ONLY_BBO_MISSING"
    assert result["total_pnl"] is None


def test_exit_only_missing_or_stale_dual_bbo_is_na():
    context = _context()
    stale = _evidence(context, near_ts=NOW_MS - 16_000,
                      far_ts=NOW_MS - 15_500)

    assert exit_only_upl_presentation(context, None, now_ms=NOW_MS)["reason"] \
        == "EXIT_ONLY_BBO_MISSING"
    assert exit_only_upl_presentation(context, stale, now_ms=NOW_MS)["reason"] \
        == "EXIT_ONLY_BBO_STALE"


def test_exit_only_bbo_code_or_identity_mismatch_is_na():
    context = _context()
    wrong_code = _evidence(context)
    wrong_code["bbo_payload"]["near"]["symbol"] = "TMFZ9"
    wrong_code["bbo_hash"] = hashlib.sha256(json.dumps(
        wrong_code["bbo_payload"], sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()
    assert exit_only_upl_presentation(context, wrong_code, now_ms=NOW_MS)["reason"] \
        == "EXIT_ONLY_SYMBOL_MISMATCH"

    wrong_identity = _evidence(context)
    wrong_identity["bbo_payload"]["session_id"] = "other-session"
    wrong_identity["bbo_hash"] = hashlib.sha256(json.dumps(
        wrong_identity["bbo_payload"], sort_keys=True, separators=(",", ":"),
        allow_nan=False).encode()).hexdigest()
    assert exit_only_upl_presentation(context, wrong_identity, now_ms=NOW_MS)["reason"] \
        == "EXIT_ONLY_IDENTITY_MISMATCH"


def test_paper_context_is_not_intercepted_by_exit_only_presentation():
    paper = {
        "requested_mode": "paper", "effective_mode": "paper_active",
        "live_order_allowed": False,
    }

    assert exit_only_upl_presentation(paper, None, now_ms=NOW_MS) is None
