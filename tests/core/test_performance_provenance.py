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
    # live scope with a config_hash drift => N/A (fully-proven live
    # record whose hash != the active sealed profile)
    fills = _write_ledger(tmp_path,
                          _live_records(config_hash="ff" * 32))
    ev2 = classify_mts_evidence(fills, None)
    assert ev2["evidence_mode"] == "live", ev2
    scope2 = scope_mts_performance(_live_rt(), ev2)
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


def _live_records(**over):
    """A fully-proven live record (the canonical shape the gate requires)."""
    base = {"trade_id": "t1", "mode": "live", "run_id": "run-1",
            "config_hash": "ab" * 32, "session_id": "sess-1",
            "source": "live_broker_reconciled"}
    base.update(over)
    return [base]


def _live_rt():
    return _rt(is_live_runtime=True, config_hash="ab" * 32,
               session_id="sess-1")


def test_live_evidence_fully_proven_ok(tmp_path):
    """The ONLY accepted live shape: every record mode=live + exactly one
    run_id + one config_hash == active + one session == active +
    source=live_broker_reconciled."""
    fills = _write_ledger(tmp_path, _live_records())
    ev = classify_mts_evidence(fills, None)
    assert ev["evidence_mode"] == "live"
    scope = scope_mts_performance(_live_rt(), ev)
    assert scope["ok"] and scope["run_id"] == "run-1"


@pytest.mark.parametrize("broken", [
    {"run_id": None},                      # missing run_id
    {"config_hash": None},                 # missing config_hash
    {"session_id": None},                  # missing session
    {"source": None},                      # missing source
    {"mode": None},                        # missing mode on a live record
])
def test_live_evidence_missing_field_is_na(tmp_path, broken):
    """Every live record MUST carry the full provenance — ANY missing
    field (run_id/config_hash/session/source/mode) => N/A."""
    rec = {k: v for k, v in broken.items() if v is None}
    fills = _write_ledger(tmp_path, _live_records(**rec))
    ev = classify_mts_evidence(fills, None)
    assert ev["evidence_mode"] != "live", ev
    scope = scope_mts_performance(_live_rt(), ev)
    assert not scope["ok"]


def test_live_evidence_multiple_run_ids_na(tmp_path):
    """Two distinct run_ids in a live ledger => N/A (no merged runs)."""
    recs = [_live_records()[0],
            _live_records(trade_id="t2", run_id="run-2")[0]]
    fills = _write_ledger(tmp_path, recs)
    ev = classify_mts_evidence(fills, None)
    assert ev["evidence_mode"] != "live"
    scope = scope_mts_performance(_live_rt(), ev)
    assert not scope["ok"]


def test_live_evidence_session_mismatch_na(tmp_path):
    """The live session/generation must equal the ACTIVE runtime session —
    a mismatch => N/A (stale/misattributed session)."""
    fills = _write_ledger(tmp_path,
                          _live_records(session_id="stale-sess"))
    ev = classify_mts_evidence(fills, None)
    assert ev["evidence_mode"] == "live"
    scope = scope_mts_performance(_live_rt(), ev)
    assert not scope["ok"] and "session" in scope["reason"]


def test_live_evidence_config_hash_mismatch_na(tmp_path):
    """The live config_hash must equal the ACTIVE sealed profile hash —
    a mismatch => N/A."""
    fills = _write_ledger(tmp_path,
                          _live_records(config_hash="ff" * 32))
    ev = classify_mts_evidence(fills, None)
    scope = scope_mts_performance(_live_rt(), ev)
    assert not scope["ok"] and "config_hash" in scope["reason"]


def test_live_evidence_wrong_source_na(tmp_path):
    """source must be exactly live_broker_reconciled — any other/missing
    source => N/A (the evidence is not broker-reconciled)."""
    fills = _write_ledger(tmp_path,
                          _live_records(source="telemetry_writer"))
    ev = classify_mts_evidence(fills, None)
    assert ev["evidence_mode"] != "live"
    scope = scope_mts_performance(_live_rt(), ev)
    assert not scope["ok"]


def test_paper_never_admits_live(tmp_path):
    """Paper scope stays compatible with paper/legacy evidence but NEVER
    admits live evidence."""
    fills = _write_ledger(tmp_path, _live_records())
    ev = classify_mts_evidence(fills, None)
    scope = scope_mts_performance(
        _rt(runtime_status="PAPER_ACTIVE", is_paper_runtime=True,
            config_hash="cd" * 32), ev)
    assert not scope["ok"] and "paper view with live" in scope["reason"]


# --- select_mts_reporting_day: must never pin the panel to a day older
# than the ledger's newest day (2026-10-16 regression: dashboard showed
# +331.1/2026-10-13 for the entire 00:00-07:59 tail of 2026-10-14's night
# session, because the caller passed a "current day" one trading day
# behind the ledger's own 15:00-rollover bucketing) ---

from core.performance_provenance import select_mts_reporting_day


def _fill(ts, net=0):
    return {"timestamp": ts, "session": "night", "net_pnl": net}


def test_select_mts_reporting_day_never_pins_behind_ledger_newest(tmp_path):
    """Ledger already has 10-14 records; even if the caller's current-day
    clock is stale (still says 10-13), the selector must not return the
    older day -- it must surface the newest day the ledger actually has."""
    fills = _write_ledger(tmp_path, [
        _fill("2026-10-13T10:00:00", net=100),
        _fill("2026-10-13T20:17:00", net=-50),  # night session -> 10-14
    ])
    stale_current_day = "2026-10-13"  # caller's clock disagrees with ledger
    reporting_day = select_mts_reporting_day(fills, None, stale_current_day)
    assert reporting_day == "2026-10-14", (
        "selector pinned the panel to a day older than the ledger's "
        f"newest day; got {reporting_day!r}")


def test_select_mts_reporting_day_still_falls_back_when_current_has_no_records(tmp_path):
    """Unrelated behavior must survive: when the (correct) current day has
    no records yet and no newer day exists either, fall back to the
    latest prior day."""
    fills = _write_ledger(tmp_path, [
        _fill("2026-10-08T10:00:00", net=100),
    ])
    reporting_day = select_mts_reporting_day(fills, None, "2026-10-13")
    assert reporting_day == "2026-10-08"


def test_select_mts_reporting_day_exact_match_still_wins(tmp_path):
    """Unrelated behavior must survive: current day with records is
    returned as-is."""
    fills = _write_ledger(tmp_path, [
        _fill("2026-10-13T10:00:00", net=100),
    ])
    reporting_day = select_mts_reporting_day(fills, None, "2026-10-13")
    assert reporting_day == "2026-10-13"
