#!/usr/bin/env python3
"""Isolated tests for the read-only Policy J attribution audit script (v6)."""

import json
from pathlib import Path
from unittest.mock import patch

from scripts.research.pj_single_leg_attribution.audit import (
    build_artifact,
    load_snapshot,
    validate_fills_schema,
    validate_events_schema,
    select_candidates,
    SnapshotLoadError,
)


def _fill(tid, leg, side, ft, price, qty=1, ts="2026-08-06T09:14:03+08:00"):
    return {"trade_id": tid, "timestamp": ts, "leg": leg, "contract": leg,
            "side": side, "fill_type": ft, "qty": qty, "price": price}


def _event(ev, ts="2026-08-06T09:14:03+08:00", tid=None, durable_peak=None, **extra):
    d = {"event": ev, "ts": ts}
    if tid is not None:
        d["trade_id"] = tid
    if durable_peak is not None:
        d["durable_peak"] = durable_peak
    d.update(extra)
    return d


def _write(tmp: Path, fills=None, events=None):
    fp = tmp / "fills.jsonl"
    ep = tmp / "events.jsonl"
    fp.write_text("\n".join(json.dumps(f, ensure_ascii=False) for f in (fills or [])) + "\n")
    ep.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in (events or [])) + "\n")
    return fp, ep


_MIN_EVENTS = [_event("EXIT_LOG")]


def _single_leg_trade(tid="mts-auto-T1", t0="2026-08-06T09:00:00+08:00"):
    """Entry both legs + release NEAR + exit FAR (tz-clean)."""
    return [
        _fill(tid, "NEAR", "SHORT", "ENTRY", 44251.0, ts=t0),
        _fill(tid, "FAR", "LONG", "ENTRY", 44177.0, ts=t0),
        _fill(tid, "NEAR", "BUY", "RELEASE", 44280.0, ts="2026-08-06T10:00:00+08:00"),
        _fill(tid, "FAR", "SELL", "EXIT", 44359.0, ts="2026-08-06T10:05:00+08:00"),
    ]


def _audit(tmp, fills, events):
    fp, ep = _write(tmp, fills, events)
    return build_artifact(fp, ep, tmp / "out", repo_root=tmp)


# ── schema ────────────────────────────────────────────────────────────────

def test_unknown_fill_type_is_unreadable(tmp_path):
    art = _audit(tmp_path, [_fill("t1", "NEAR", "SHORT", "FUTURES_TEST_NEW", 44251.0)], [])
    assert art["status"] == "UNREADABLE"
    assert art["schema_mismatch"]["fills"]["reason"] == "unknown_fill_type"


def test_explicit_allowlist_no_wildcard(tmp_path):
    # a COMBINED_EXIT-style new prefix must NOT be tolerated (v6: no wildcard)
    art = _audit(tmp_path, [_fill("t1", "NEAR", "SELL", "COMBINED_EXIT_CUSTOM", 44251.0)], [])
    assert art["status"] == "UNREADABLE"


def test_malformed_line_is_unreadable(tmp_path):
    fp, ep = _write(tmp_path, [])
    fp.write_text('{"trade_id": "t1", "broken json\n')
    art = build_artifact(fp, ep, tmp_path / "out", repo_root=tmp_path)
    assert art["status"] == "SNAPSHOT_MALFORMED"


def test_missing_core_key_unreadable(tmp_path):
    bad = _fill("t1", "NEAR", "SHORT", "ENTRY", 44251.0)
    del bad["price"]
    art = _audit(tmp_path, [bad], [])
    assert art["status"] == "UNREADABLE"
    assert art["schema_mismatch"]["fills"]["reason"] == "missing_core_keys"


def test_bad_side_unreadable(tmp_path):
    art = _audit(tmp_path, [_fill("t1", "NEAR", "NEAR", "ENTRY", 44251.0)], [])
    assert art["status"] == "UNREADABLE"
    assert art["schema_mismatch"]["fills"]["reason"] == "side_out_of_allowlist"


def test_combined_exit_side_buy_sell_allowed(tmp_path):
    # observed: COMBINED_EXIT rows carry BUY/SELL sides — must NOT be unreadable
    art = _audit(tmp_path, [_fill("t1", "NEAR", "BUY", "COMBINED_EXIT", 44251.0)], _MIN_EVENTS)
    assert art["status"] == "OK"


def test_test_rows_recorded_not_unreadable(tmp_path):
    art = _audit(tmp_path, [_fill("tX", "NEAR", "SELL", "TEST", 1.0)], _MIN_EVENTS)
    assert art["status"] == "OK"
    assert art["manifest"]["inputs"]["fills"]["source_schema"]["test_rows"] == 1


def test_global_events_without_trade_id_ok(tmp_path):
    fills = _single_leg_trade()
    events = [_event("EXIT_LOG"), _event("POLICY_J_TRIGGER_SUPPRESSED")]
    art = _audit(tmp_path, fills, events)
    assert art["status"] == "OK"
    assert art["manifest"]["inputs"]["events"]["source_schema"][
        "global_events_without_trade_id"] == 2


def test_per_trade_evidence_missing_trade_id_counted(tmp_path):
    fills = _single_leg_trade()
    events = [_event("POLICY_J_PEAK_CONFIRMED", durable_peak=300)]  # no trade_id
    art = _audit(tmp_path, fills, events)
    assert art["status"] == "OK"
    assert art["manifest"]["inputs"]["events"]["source_schema"][
        "per_trade_evidence_missing_trade_id"].get("POLICY_J_PEAK_CONFIRMED") == 1


# ── candidates ─────────────────────────────────────────────────────────────

def test_candidate_selected_for_complete_single_leg(tmp_path):
    fills = _single_leg_trade()
    cands, rejected = select_candidates(fills)
    assert len(cands) == 1
    assert cands[0]["released_leg"] == "NEAR" and cands[0]["remaining_leg"] == "FAR"
    assert rejected == {}


def test_test_contamination_excludes_whole_trade(tmp_path):
    fills = _single_leg_trade("mts-auto-T2") + [
        _fill("mts-auto-T2", "NEAR", "SELL", "TEST", 1.0, ts="2026-08-06T09:30:00+08:00")]
    cands, rejected = select_candidates(fills)
    assert cands == []
    assert rejected.get("TEST_TRADE_CONTAMINATION") == 1


def test_combined_exit_trade_rejected(tmp_path):
    fills = _single_leg_trade("mts-auto-T3") + [
        _fill("mts-auto-T3", "FAR", "SELL", "COMBINED_EXIT", 44300.0,
              ts="2026-08-06T11:00:00+08:00")]
    cands, rejected = select_candidates(fills)
    assert cands == []
    assert rejected.get("COMBINED_EXIT") == 1


def test_qty_mismatch_rejected(tmp_path):
    fills = _single_leg_trade("mts-auto-T4")
    fills[0]["qty"] = 2  # ENTRY qty=2 → must be rejected
    cands, rejected = select_candidates(fills)
    assert cands == []
    assert rejected.get("QTY_MISMATCH", 0) >= 1 or rejected.get("BAD_SIDE", 0) >= 1


# ── classification ─────────────────────────────────────────────────────────

def test_incident_reproduction_not_provable_without_decision(tmp_path):
    fills = _single_leg_trade()
    art = _audit(tmp_path, fills, _MIN_EVENTS)
    assert art["status"] == "OK"
    assert art["summary"]["SUPPORTED"]["PROVEN"] == 0
    assert art["summary"]["CONTRADICTED"] == 0
    assert art["trades"][0]["classification"] == "INSUFFICIENT_EVIDENCE"
    assert art["trades"][0]["attribution_strength"] == "NOT_PROVABLE"


def test_inferred_eligible_with_resolved_params_and_peak(tmp_path):
    fills = _single_leg_trade()
    # codex A3: eligibility requires a timestamped same-trade decision record
    # with BOTH durable_peak AND current_net_twd at that moment.
    events = [
        _event("POLICY_J_PEAK_CONFIRMED", ts="2026-08-06T10:03:00+08:00",
               tid="mts-auto-T1", durable_peak=300.0, current_net_twd=-40.0),
    ]
    fp, ep = _write(tmp_path, fills, events)
    with patch("scripts.research.pj_single_leg_attribution.audit.resolve_params",
               return_value={"param_source": "DEPLOYED_CONFIG_abc",
                             "activation_twd": 200, "giveback_twd": 50,
                             "mult": 10, "friction": 92}):
        art = build_artifact(fp, ep, tmp_path / "out", repo_root=tmp_path)
    assert art["summary"]["INSUFFICIENT_EVIDENCE"]["INFERRED_ELIGIBLE"] == 1
    assert art["trades"][0]["attribution_strength"] == "INFERRED_ELIGIBLE"
    assert art["trades"][0]["eligibility_consistent"] is True


def test_naive_ts_not_provable_eligibility_null(tmp_path):
    fills = _single_leg_trade()
    for f in fills:
        f["timestamp"] = f["timestamp"].replace("+08:00", "")  # naive
    events = [_event("POLICY_J_PEAK_CONFIRMED", ts="2026-08-06T10:03:00",
                     tid="mts-auto-T1", durable_peak=300.0)]
    fp, ep = _write(tmp_path, fills, events)
    with patch("scripts.research.pj_single_leg_attribution.audit.resolve_params",
               return_value={"param_source": "DEPLOYED_CONFIG_abc",
                             "activation_twd": 200, "giveback_twd": 50,
                             "mult": 10, "friction": 92}):
        art = build_artifact(fp, ep, tmp_path / "out", repo_root=tmp_path)
    assert art["trades"][0]["attribution_strength"] == "NOT_PROVABLE"
    assert art["trades"][0]["eligibility_consistent"] is None
    assert "TS_NAIVE" in art["trades"][0]["source_limits"]


def test_order_violation_not_provable(tmp_path):
    fills = _single_leg_trade()
    fills[1]["timestamp"] = "2026-08-06T11:00:00+08:00"  # far entry after release/exit
    art = _audit(tmp_path, fills, _MIN_EVENTS)
    assert art["trades"][0]["attribution_strength"] == "NOT_PROVABLE"
    assert "ORDER_VIOLATION" in art["trades"][0]["source_limits"]


# ── end-to-end ─────────────────────────────────────────────────────────────

def test_full_run_writes_artifact(tmp_path, monkeypatch):
    from scripts.research.pj_single_leg_attribution import audit
    fills = _single_leg_trade()
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    (logs / "mts_trade_fills.jsonl").write_text(
        "\n".join(json.dumps(f, ensure_ascii=False) for f in fills) + "\n")
    (logs / "mts_spread_events.jsonl").write_text(
        json.dumps(_event("EXIT_LOG"), ensure_ascii=False) + "\n")
    out = tmp_path / "out"
    monkeypatch.setattr("sys.argv", ["audit.py", "--output-dir", str(out),
                                     "--runtime", str(tmp_path)])
    monkeypatch.setattr(audit, "DEFAULT_RUNTIME", str(tmp_path))
    rc = audit.main()
    assert rc == 0
    arts = list(out.glob("pj_single_leg_attribution_*.json"))
    assert len(arts) == 1
    data = json.loads(arts[0].read_text())
    assert data["status"] == "OK"
    assert data["summary"]["SUPPORTED"]["PROVEN"] == 0


# ── codex audit round 2: RED tests (strictness fixes, test-first) ─────────

def test_invalid_utf8_inside_json_is_malformed(tmp_path):
    # strict UTF-8: invalid bytes must make the WHOLE snapshot malformed,
    # not be silently replaced
    fp, ep = _write(tmp_path, [])
    fp.write_bytes(b'{"trade_id": "t1", "leg": "NEAR", "contract": "NEAR", '
                   b'"side": "SHORT", "fill_type": "ENTRY", "qty": 1, '
                   b'"price": 1, "timestamp": "2026-08-06T09:00:00+08:00", '
                   b'"note": "\xff\xfe"}\n')
    art = build_artifact(fp, ep, tmp_path / "out", repo_root=tmp_path)
    assert art["status"] == "SNAPSHOT_MALFORMED"


def test_schema_keys_checked_on_every_row(tmp_path):
    fills = _single_leg_trade()
    fills.append(_fill("mts-auto-T5", "NEAR", "SHORT", "ENTRY", 44252.0,
                       ts="2026-08-06T09:30:00+08:00"))
    del fills[-1]["price"]
    art = _audit(tmp_path, fills, _MIN_EVENTS)
    assert art["status"] == "UNREADABLE"
    assert art["schema_mismatch"]["fills"]["reason"] == "missing_core_keys"


def test_nan_qty_is_malformed(tmp_path):
    # python json.loads accepts NaN by default — must be rejected as malformed
    fp, ep = _write(tmp_path, [])
    fp.write_text('{"trade_id": "t1", "leg": "NEAR", "contract": "NEAR", '
                  '"side": "SHORT", "fill_type": "ENTRY", "qty": NaN, '
                  '"price": 1, "timestamp": "2026-08-06T09:00:00+08:00"}\n')
    art = build_artifact(fp, ep, tmp_path / "out", repo_root=tmp_path)
    assert art["status"] == "SNAPSHOT_MALFORMED"


def test_fractional_qty_is_malformed(tmp_path):
    fp, ep = _write(tmp_path, [])
    fp.write_text('{"trade_id": "t1", "leg": "NEAR", "contract": "NEAR", '
                  '"side": "SHORT", "fill_type": "ENTRY", "qty": 1.5, '
                  '"price": 1, "timestamp": "2026-08-06T09:00:00+08:00"}\n')
    art = build_artifact(fp, ep, tmp_path / "out", repo_root=tmp_path)
    assert art["status"] == "SNAPSHOT_MALFORMED"


def test_mixed_aware_naive_timestamps_no_crash(tmp_path):
    fills = _single_leg_trade()
    fills[0]["timestamp"] = fills[0]["timestamp"].replace("+08:00", "")  # naive
    art = _audit(tmp_path, fills, _MIN_EVENTS)
    assert art["status"] == "OK"          # must not crash on aware/naive compare
    assert art["trades"][0]["attribution_strength"] == "NOT_PROVABLE"
    assert "TS_NAIVE" in art["trades"][0]["source_limits"]


def test_trigger_detection_excludes_suppressed(tmp_path):
    # TRIGGER_SUPPRESSED is NOT a trigger decision → NO_TRIGGER reason must fire
    fills = _single_leg_trade()
    events = [_event("POLICY_J_TRIGGER_SUPPRESSED"),
              _event("EXIT_LOG")]
    art = _audit(tmp_path, fills, events)
    assert "NO_TRIGGER_NAMED_EVENT_IN_SCHEMA" in art["summary"]["reasons"]


def test_final_cause_marker_inspected_as_field(tmp_path):
    # a final-decision cause marker is a FIELD (cause=...) on an event row,
    # not a literal "cause" string value
    fills = _single_leg_trade()
    events = [_event("RELEASE_NEAR_SUBMITTED", tid="mts-auto-T1", cause="trail"),
              _event("EXIT_LOG")]
    art = _audit(tmp_path, fills, events)
    assert "NO_FINAL_DECISION_CAUSE_EVENT" not in art["summary"]["reasons"]


def test_release_snapshot_alone_is_not_eligibility(tmp_path):
    """codex A3 counterexample: the RELEASE row PnL satisfies the giveback
    condition, but no timestamped decision record carries the net at a later
    moment → NOT_PROVABLE / NO_DECISION_TIME_MARK (never INFERRED_ELIGIBLE)."""
    fills = _single_leg_trade()
    fills[2]["near_pnl"] = 300.0   # release-time snapshot satisfies conditions
    fills[2]["far_pnl"] = 0.0
    events = [_event("POLICY_J_PEAK_CONFIRMED", ts="2026-08-06T10:03:00+08:00",
                     tid="mts-auto-T1", durable_peak=300.0)]  # NO current_net_twd
    fp, ep = _write(tmp_path, fills, events)
    with patch("scripts.research.pj_single_leg_attribution.audit.resolve_params",
               return_value={"param_source": "DEPLOYED_CONFIG_abc",
                             "activation_twd": 200, "giveback_twd": 50,
                             "mult": 10, "friction": 92}):
        art = build_artifact(fp, ep, tmp_path / "out", repo_root=tmp_path)
    trade = art["trades"][0]
    assert trade["attribution_strength"] == "NOT_PROVABLE"
    assert trade["eligibility_consistent"] is None
    assert "NO_DECISION_TIME_MARK" in trade["source_limits"]


def test_default_repo_root_is_actual_repo(tmp_path):
    # repo_root must default to the script's own repository, not runtime.parent
    fills = _single_leg_trade()
    fp, ep = _write(tmp_path, fills, _MIN_EVENTS)
    art = build_artifact(fp, ep, tmp_path / "out")   # no repo_root arg
    sha = art.get("script_commit_sha", "")
    assert len(sha) == 40 and sha != "unknown", f"got sha={sha!r}"


# ── codex audit round 3: A1-A5 RED tests ───────────────────────────────────

def test_trigger_named_event_requires_trade_id(tmp_path):
    # A1: a trigger-named event WITHOUT trade_id must NOT count as provenance
    fills = _single_leg_trade()
    events = [_event("POLICY_J_TRIGGERED"),            # no trade_id
              _event("POLICY_J_SINGLE_LEG_TRIGGERED"),  # no trade_id
              _event("EXIT_LOG")]
    art = _audit(tmp_path, fills, events)
    assert "NO_TRIGGER_NAMED_EVENT_IN_SCHEMA" in art["summary"]["reasons"]


def test_unknown_trigger_name_never_counts(tmp_path):
    # A1: arbitrary *TRIGGER* names must not count — only the explicit set
    fills = _single_leg_trade()
    events = [_event("POLICY_J_TRIGGER_CUSTOM", tid="mts-auto-T1"),
              _event("EXIT_LOG")]
    art = _audit(tmp_path, fills, events)
    assert "NO_TRIGGER_NAMED_EVENT_IN_SCHEMA" in art["summary"]["reasons"]


def test_final_cause_requires_same_trade_and_type(tmp_path):
    # A2: unrelated rows (wrong event type / missing trade_id) must not clear
    # the provenance gap
    fills = _single_leg_trade()
    events = [
        _event("MARKET_DATA_TRACE", cause="trail"),          # not a final-decision type
        _event("RELEASE_NEAR_SUBMITTED", cause="trail"),      # no trade_id
        _event("EXIT_SUBMITTED", tid="mts-auto-OTHER", cause="trail"),  # different trade
        _event("EXIT_LOG"),
    ]
    art = _audit(tmp_path, fills, events)
    assert "NO_FINAL_DECISION_CAUSE_EVENT" in art["summary"]["reasons"], \
        "only same-trade explicit final-decision cause events clear the gap"


def test_event_rows_qty_not_checked(tmp_path):
    # A4: qty validation applies only where qty semantics are defined (fills);
    # an event row carrying a stray qty must not make the snapshot malformed
    fills = _single_leg_trade()
    events = [_event("EXIT_LOG", qty=1.5)]
    art = _audit(tmp_path, fills, events)
    assert art["status"] == "OK"


def test_malformed_failure_preserves_snapshot_evidence(tmp_path):
    # A4: failure artifact keeps input path + parsed-byte hash + byte count
    fp, ep = _write(tmp_path, [])
    fp.write_text('{"trade_id": "t1", "broken json\n')
    art = build_artifact(fp, ep, tmp_path / "out", repo_root=tmp_path)
    assert art["status"] == "SNAPSHOT_MALFORMED"
    assert art["input_path"] == str(fp)
    assert isinstance(art.get("input_sha256"), str) and len(art["input_sha256"]) == 64
    assert isinstance(art.get("input_bytes"), int) and art["input_bytes"] > 0
    assert "parser_error" in art


def test_provenance_gap_only_flag(tmp_path):
    # A5: provenance-gap-only baseline is declared explicitly
    fills = _single_leg_trade()
    art = _audit(tmp_path, fills, _MIN_EVENTS)
    assert art["summary"]["provenance_gap_only"] is True
    assert art["manifest"]["params"]["param_source"] == "PARAMETER_VERSION_UNKNOWN"
