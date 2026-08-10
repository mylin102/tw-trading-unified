"""RED/GREEN: MTS Paper/Live performance provenance contract.

Canonical presentation rules (fail-closed — NEVER 0 or merged PnL on
uncertainty):
  - Live view uses ONLY live broker-reconciled evidence; Paper view ONLY
    paper evidence
  - legacy/mixed/unknown evidence => N/A + explicit source-mismatch
    reason
  - live verified-flat UPL => 0 ONLY with a fresh snapshot timestamp;
    missing/stale => N/A
  - no Paper/Live mixing
"""

import json
import time
from pathlib import Path

import pytest

from core.performance_provenance import (
    classify_mts_evidence, scope_mts_performance, upl_presentation)


def _rt(**over):
    base = {"runtime_status": "LIVE_READY", "profile_identity": "futures_live.yaml",
            "requested_mode": "live", "effective_mode": "live_ready",
            "live_order_allowed": True, "config_hash": "ab" * 32,
            "is_live_runtime": False, "is_paper_runtime": False}
    base.update(over)
    return base


def _write_ledger(tmp_path, records, name="mts_trade_fills.jsonl"):
    p = tmp_path / name
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n",
                 encoding="utf-8")
    return str(p)


def test_live_view_with_paper_evidence_is_na(tmp_path):
    """Live runtime + paper (or legacy) ledger => scope N/A + explicit
    source-mismatch reason (never merged, never 0)."""
    fills = _write_ledger(tmp_path, [{"trade_id": "t1", "mode": "paper"}])
    ev = classify_mts_evidence(fills, None)
    assert ev["evidence_mode"] == "paper"
    scope = scope_mts_performance(_rt(is_live_runtime=True), ev)
    assert not scope["ok"]
    assert "source mismatch" in scope["reason"]
    # legacy (no per-record mode) is equally unusable for a live view
    fills2 = _write_ledger(tmp_path, [{"trade_id": "t1"}], "legacy.jsonl")
    ev2 = classify_mts_evidence(fills2, None)
    assert ev2["evidence_mode"] == "legacy"
    scope2 = scope_mts_performance(_rt(is_live_runtime=True), ev2)
    assert not scope2["ok"] and "legacy" in scope2["reason"]


def test_paper_view_with_paper_evidence_ok(tmp_path):
    """Paper runtime + paper (or legacy) ledger => scope ok; the metrics
    may render from the paper evidence (paper compatibility preserved)."""
    fills = _write_ledger(tmp_path, [{"trade_id": "t1", "mode": "paper"}])
    ev = classify_mts_evidence(fills, None)
    scope = scope_mts_performance(
        _rt(runtime_status="PAPER_ACTIVE", is_paper_runtime=True,
            config_hash="cd" * 32), ev)
    assert scope["ok"] and scope["mode"] == "paper"
    # legacy records are the paper evidence (pre-provenance era)
    fills2 = _write_ledger(tmp_path, [{"trade_id": "t1"}], "legacy.jsonl")
    ev2 = classify_mts_evidence(fills2, None)
    scope2 = scope_mts_performance(
        _rt(runtime_status="PAPER_ACTIVE", is_paper_runtime=True,
            config_hash="cd" * 32), ev2)
    assert scope2["ok"] and scope2["reason"] is None


def test_live_flat_upl_zero_only_with_fresh_snapshot(tmp_path):
    """Live verified-flat UPL => 0 ONLY with a fresh snapshot timestamp;
    missing or stale snapshot => N/A (cannot verify the flat claim)."""
    scope = scope_mts_performance(
        _rt(is_live_runtime=True, config_hash="ab" * 32),
        {"evidence_mode": "live", "run_ids": ["r1"],
         "config_hashes": ["ab" * 32], "record_count": 2})
    assert scope["ok"]
    # fresh snapshot => 0
    fresh = time.time()
    p = upl_presentation(scope, is_flat=True, snapshot_ts=fresh)
    assert p["kind"] == "ZERO" and p["value"] == 0.0
    # stale snapshot => N/A
    p2 = upl_presentation(scope, is_flat=True,
                          snapshot_ts=time.time() - 3600)
    assert p2["kind"] == "NA" and "stale" in p2["reason"]
    # missing snapshot => N/A
    p3 = upl_presentation(scope, is_flat=True, snapshot_ts=None)
    assert p3["kind"] == "NA" and "missing" in p3["reason"]
    # non-flat => COMPUTED (the caller renders the scoped value)
    p4 = upl_presentation(scope, is_flat=False, snapshot_ts=fresh)
    assert p4["kind"] == "COMPUTED"


def test_missing_or_stale_evidence_na(tmp_path):
    """Missing ledger => N/A; a stale live scope (config_hash mismatch) =>
    N/A with the source-mismatch reason."""
    ev = classify_mts_evidence(str(tmp_path / "nope.jsonl"), None)
    assert ev["evidence_mode"] == "missing"
    scope = scope_mts_performance(_rt(is_live_runtime=True), ev)
    assert not scope["ok"] and "missing" in scope["reason"]
    # live scope with a config_hash drift => N/A
    fills = _write_ledger(tmp_path, [{"trade_id": "t1", "mode": "live",
                                      "config_hash": "ff" * 32}])
    ev2 = classify_mts_evidence(fills, None)
    scope2 = scope_mts_performance(_rt(is_live_runtime=True,
                                       config_hash="ab" * 32), ev2)
    assert not scope2["ok"] and "config_hash" in scope2["reason"]


def test_no_paper_live_mixing(tmp_path):
    """A ledger mixing paper and live records => N/A + explicit reason
    (never merged PnL)."""
    fills = _write_ledger(tmp_path, [
        {"trade_id": "t1", "mode": "paper"},
        {"trade_id": "t2", "mode": "live"},
    ])
    ev = classify_mts_evidence(fills, None)
    assert ev["evidence_mode"] == "mixed"
    scope = scope_mts_performance(_rt(is_live_runtime=True), ev)
    assert not scope["ok"] and "cannot attribute" in scope["reason"]
    scope2 = scope_mts_performance(
        _rt(runtime_status="PAPER_ACTIVE", is_paper_runtime=True), ev)
    assert not scope2["ok"]


def test_unknown_runtime_fail_closed(tmp_path):
    """An unknown/quarantined runtime => N/A regardless of the ledger."""
    fills = _write_ledger(tmp_path, [{"trade_id": "t1", "mode": "live"}])
    ev = classify_mts_evidence(fills, None)
    scope = scope_mts_performance(_rt(runtime_status="UNKNOWN / QUARANTINED"),
                                  ev)
    assert not scope["ok"] and "fail-closed" in scope["reason"]
