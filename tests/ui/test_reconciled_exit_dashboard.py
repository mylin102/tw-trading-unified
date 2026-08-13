"""Dashboard-only presentation contract for RECONCILED_EXIT_ONLY.

The restricted recovery mode must never reuse paper/local MTS state. Its
UPL is presentable only from the broker-attested capability and a current,
hash-bound dual-leg Shioaji BBO payload.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

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


def _rebound(evidence):
    """Recompute the hash after a payload mutation.  allow_nan=True
    because the deliberately-injected NaN case is exactly what the quote
    validation must reject (production payloads never contain NaN)."""
    evidence["bbo_hash"] = hashlib.sha256(json.dumps(
        evidence["bbo_payload"], sort_keys=True, separators=(",", ":"),
        allow_nan=True).encode()).hexdigest()
    return evidence


def test_exit_only_untrusted_evidence_source_or_quotes_is_na():
    """[Dashboard] the event JSONL is untrusted: BBO source must be
    shioaji_bidask and bid/ask must be finite positive with bid <= ask —
    otherwise N/A with the typed reason (never a crash)."""
    from core.exit_only_position import _json_safe  # noqa: F401
    from ui.reconciled_exit_presentation import (
        exit_only_upl_presentation)

    ctx = _context()
    # wrong source
    bad_src = _evidence(ctx)
    bad_src["bbo_payload"]["near"]["source"] = "tick_cache"
    _rebound(bad_src)
    assert exit_only_upl_presentation(
        ctx, bad_src, now_ms=NOW_MS)["reason"] \
        == "EXIT_ONLY_SOURCE_MISMATCH"
    # NaN bid: not canonically hashable (allow_nan=False) => hash check
    # blocks it as corrupted evidence (typed N/A, never a crash)
    nan_bid = _evidence(ctx)
    nan_bid["bbo_payload"]["near"]["bid"] = float("nan")
    _rebound(nan_bid)
    assert exit_only_upl_presentation(
        ctx, nan_bid, now_ms=NOW_MS)["reason"] \
        == "EXIT_ONLY_BBO_HASH_MISMATCH"
    # non-positive ask
    neg_ask = _evidence(ctx)
    neg_ask["bbo_payload"]["far"]["ask"] = -1.0
    _rebound(neg_ask)
    assert exit_only_upl_presentation(
        ctx, neg_ask, now_ms=NOW_MS)["reason"] \
        == "EXIT_ONLY_BBO_INVALID"
    # bid > ask
    crossed = _evidence(ctx)
    crossed["bbo_payload"]["near"]["bid"] = 102.0
    _rebound(crossed)
    assert exit_only_upl_presentation(
        ctx, crossed, now_ms=NOW_MS)["reason"] \
        == "EXIT_ONLY_BBO_INVALID"
    # valid evidence still computes
    ok = exit_only_upl_presentation(ctx, _evidence(ctx), now_ms=NOW_MS)
    assert ok["kind"] == "COMPUTED" and ok["total_pnl"] == 60.0


def test_exit_only_upl_metrics_helper(tmp_path):
    """[Dashboard] exit_only_upl_metrics wires the event-ledger evidence
    scan + presentation: valid ledger -> COMPUTED; empty ledger -> NA
    BBO_MISSING; non-EXIT_ONLY context -> None."""
    from ui.reconciled_exit_presentation import exit_only_upl_metrics

    ctx = _context()
    p = tmp_path / "events.jsonl"
    assert exit_only_upl_metrics(ctx, str(p), now_ms=NOW_MS)[
        "reason"] == "EXIT_ONLY_BBO_MISSING"
    p.write_text(json.dumps(
        {"event": "ORDER_SUBMITTED", "bbo_hash": "x",
         "bbo_payload": {"v": 1}}) + "\n")
    ev = _evidence(ctx)
    p.write_text(json.dumps({"event": "ORDER_SUBMITTED", **ev}) + "\n")
    res = exit_only_upl_metrics(ctx, str(p), now_ms=NOW_MS)
    assert res["kind"] == "COMPUTED" and res["total_pnl"] == 60.0
    assert exit_only_upl_metrics(
        {"effective_mode": "paper_active"}, str(p), now_ms=NOW_MS) is None


def test_load_exit_only_context(tmp_path, monkeypatch):
    """[Dashboard] the primary panel reads the canonical execution
    context: only RECONCILED_EXIT_ONLY contexts are returned."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    from ui.dashboard import load_exit_only_context

    assert load_exit_only_context() == {}
    (tmp_path / "execution_context.json").write_text(json.dumps({
        "effective_mode": "reconciled_exit_only",
        "exit_only_capability": {"legs": []}}))
    ctx = load_exit_only_context()
    assert ctx["effective_mode"] == "reconciled_exit_only"
    (tmp_path / "execution_context.json").write_text(json.dumps({
        "effective_mode": "live_ready"}))
    assert load_exit_only_context() == {}


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


def test_exit_only_non_dict_leg_container_is_na():
    """[P1 gap] a malformed leg CONTAINER (None / str / list element)
    must yield typed EXIT_ONLY_CAPABILITY_INVALID — never an
    AttributeError the dashboard broad-except would swallow."""
    from ui.reconciled_exit_presentation import (
        exit_only_upl_presentation)

    for _leg in (None, "TMFH6", ["near"], 3, True):
        c = _context()
        c["exit_only_capability"] = dict(c["exit_only_capability"])
        c["exit_only_capability"]["legs"] = [_leg, _leg]
        res = exit_only_upl_presentation(
            c, None, now_ms=NOW_MS)
        assert res["kind"] == "NA", repr(_leg)
        assert res["reason"] == "EXIT_ONLY_CAPABILITY_INVALID", repr(_leg)
        assert res["total_pnl"] is None, repr(_leg)
    # one dict + one non-dict is also typed NA
    c = _context()
    c["exit_only_capability"] = dict(c["exit_only_capability"])
    c["exit_only_capability"]["legs"] = [
        dict(c["exit_only_capability"]["legs"][0]), None]
    res = exit_only_upl_presentation(c, None, now_ms=NOW_MS)
    assert res["reason"] == "EXIT_ONLY_CAPABILITY_INVALID"


def test_exit_only_malformed_capability_leg_is_na():
    """[P1 closure] malformed capability legs (bad side, non-numeric or
    non-positive qty, non-numeric cost) => typed
    EXIT_ONLY_CAPABILITY_INVALID — never float-coerced; valid legs
    still compute."""
    from ui.reconciled_exit_presentation import (
        exit_only_upl_presentation)

    def _cap_with(**over):
        c = _context()
        c["exit_only_capability"] = dict(c["exit_only_capability"])
        legs = [dict(l) for l in c["exit_only_capability"]["legs"]]
        legs[0].update(over)
        c["exit_only_capability"]["legs"] = legs
        return c

    for _over in ({"side": "SHORT"}, {"side": "LONG"},
                  {"remaining_qty": "abc"}, {"remaining_qty": 0},
                  {"remaining_qty": -2}, {"avg_cost": None},
                  {"avg_cost": "nan"}):
        res = exit_only_upl_presentation(
            _cap_with(**_over), _evidence(_context()), now_ms=NOW_MS)
        assert res["kind"] == "NA", _over
        assert res["reason"] == "EXIT_ONLY_CAPABILITY_INVALID", _over
    ok = exit_only_upl_presentation(
        _context(), _evidence(_context()), now_ms=NOW_MS)
    assert ok["kind"] == "COMPUTED" and ok["total_pnl"] == 60.0


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


def _exit_only_ctx_dict(rid="rid-abc-123"):
    """A RECONCILED_EXIT_ONLY context dict with the given rid."""
    return {
        "effective_mode": "reconciled_exit_only",
        "exit_only_capability": {
            "reconciliation_id": rid,
            "legs": [
                {"symbol": "TMFH6", "side": "sell", "remaining_qty": 1,
                 "avg_cost": 44909.0},
                {"symbol": "TMFI6", "side": "buy", "remaining_qty": 1,
                 "avg_cost": 45052.0},
            ],
        },
    }


def test_exit_only_order_rows_current_rid_visible_others_isolated():
    """[Dashboard] in RECONCILED_EXIT_ONLY the CURRENT order table keeps
    only rows explicitly bound to the current capability rid; stale /
    no-rid / legacy RECOVERED / prior-session rows are excluded and
    counted (never deleted)."""
    from ui.dashboard import filter_mts_order_rows_for_exit_only

    rows = [
        {"order_id": "ORD-20260812-000001", "status": "rejected",
         "reconciliation_id": "rid-OLD-CAPABILITY"},
        {"order_id": "RECOV-091207", "status": "filled",
         "reconciliation_id": None},
        {"order_id": "ORD-20260812-000002", "status": "filled",
         "reconciliation_id": "rid-abc-123"},
        {"order_id": "ORD-PAPER", "status": "filled",
         "reconciliation_id": "paper"},
    ]
    kept, excluded = filter_mts_order_rows_for_exit_only(
        rows, reconciliation_id="rid-abc-123")
    assert [r["order_id"] for r in kept] == ["ORD-20260812-000002"]
    assert excluded == 3  # stale rid + no rid + paper
    # the source list is never mutated (no ledger deletion)
    assert len(rows) == 4


def test_exit_only_order_rows_no_rid_shows_nothing():
    """[Dashboard] with no matching rid, the current table shows zero
    rows and all records are counted as isolated."""
    from ui.dashboard import filter_mts_order_rows_for_exit_only

    rows = [
        {"order_id": "RECOV-091207", "reconciliation_id": None},
        {"order_id": "ORD-OLD", "reconciliation_id": "rid-old"},
    ]
    kept, excluded = filter_mts_order_rows_for_exit_only(
        rows, reconciliation_id="rid-abc-123")
    assert kept == []
    assert excluded == 2


def test_exit_only_order_visibility_paper_live_unchanged():
    """[Dashboard] exit_only_order_visibility leaves PAPER / normal LIVE
    displays untouched (no filtering, zero isolated)."""
    from ui.dashboard import exit_only_order_visibility

    rows = [
        {"order_id": "ORD-1", "status": "filled", "reconciliation_id": None},
        {"order_id": "ORD-2", "status": "rejected", "reconciliation_id": "old"},
    ]
    # paper (no exit-only context)
    shown, isolated = exit_only_order_visibility(rows, context={})
    assert shown == rows and isolated == 0
    # normal live (live_ready)
    shown, isolated = exit_only_order_visibility(
        rows, context={"effective_mode": "live_ready"})
    assert shown == rows and isolated == 0
    # exit-only filters
    shown, isolated = exit_only_order_visibility(
        rows, context=_exit_only_ctx_dict("rid-abc-123"))
    assert shown == [] and isolated == 2
    # matching rid rows stay visible in exit-only
    rows2 = [{"order_id": "ORD-NEW", "reconciliation_id": "rid-abc-123"}]
    shown, isolated = exit_only_order_visibility(
        rows2, context=_exit_only_ctx_dict("rid-abc-123"))
    assert [r["order_id"] for r in shown] == ["ORD-NEW"] and isolated == 0


def test_exit_only_renewal_status_display(tmp_path, monkeypatch):
    """[simplified] the dashboard 自動對帳 status string: healthy /
    degraded, last success, last failure, next attempt; absent file =>
    "" (nothing to show).  No dual-TTL concept."""
    import json
    from ui.dashboard import load_exit_only_renewal_status

    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    assert load_exit_only_renewal_status() == ""

    _prov = {
        "status": "ACTIVE",
        "renewed_at_ms": 1_786_000_000_000,
        "next_renewal_at_ms": 1_786_000_030_000,
        "last_failed_at_ms": None,
        "last_reason": None,
    }
    (tmp_path / "exit_only_renewal_provenance.json").write_text(
        json.dumps(_prov), encoding="utf-8")
    st = load_exit_only_renewal_status()
    assert "healthy" in st
    assert "上次成功" in st and "下次" in st
    assert "TTL" not in st  # no dual-TTL concept

    _prov["status"] = "DEGRADED"
    _prov["last_failed_at_ms"] = 1_786_000_000_500
    _prov["last_reason"] = "EXIT_ONLY_RENEWAL_QUERY_FAILED"
    (tmp_path / "exit_only_renewal_provenance.json").write_text(
        json.dumps(_prov), encoding="utf-8")
    st = load_exit_only_renewal_status()
    assert "degraded" in st
    assert "上次失敗" in st
    assert "EXIT_ONLY_RENEWAL_QUERY_FAILED" in st


def test_exit_only_primary_panel_skips_legacy_state_and_daily_jsonl():
    """The restricted-exit primary panel has a separate capability/BBO
    presentation path; it must not fall through to /tmp state or MTS daily
    performance JSONL.  LIVE/PAPER remain on their existing branch."""
    source = (Path(__file__).parents[2] / "ui" / "dashboard.py").read_text()

    assert "_mts_state_file = None if _exit_only_dashboard" in source
    assert "if not _exit_only_dashboard and os.path.exists(_fills_path):" in source
    assert "受限平倉模式—等待新鮮券商對帳與雙腿 BBO" in source


def test_existing_capability_can_build_narrow_re_attestation_payload():
    """An existing exit-only capability must show an update path whose
    request carries exactly its two currently attested legs; user input
    cannot widen codes, side, quantities, or its audit Trade ID."""
    from ui.dashboard import build_exit_only_attestation_request

    cap = _context()["exit_only_capability"]
    payload = build_exit_only_attestation_request(
        cap, operator="operator", trade_id="trade-refresh",
        evidence="fresh reconciliation requested", now_ms=NOW_MS)

    assert payload["action"] == "ATTEST_EXIT_ONLY"
    assert payload["operator"] == "operator"
    # The source capability, not an update-form caller, owns audit identity.
    assert payload["trade_id"] == "trade-1"
    # ``avg_cost`` in the capability is presentation evidence only; the
    # attestation command carries exactly the monitor's expected-leg schema.
    assert payload["expected_legs"] == [
        {"symbol": "TMFH6", "side": "sell", "remaining_qty": 2},
        {"symbol": "TMFI6", "side": "buy", "remaining_qty": 2},
    ]


def test_re_attestation_command_is_atomic_and_pending_is_not_overwritten(tmp_path):
    """The dashboard writer uses the existing O_EXCL command protocol:
    once a request is pending, an update must never replace it."""
    from ui.dashboard import (
        build_exit_only_attestation_request,
        write_exit_only_attestation_request,
    )

    cap = _context()["exit_only_capability"]
    first = build_exit_only_attestation_request(
        cap, operator="operator", trade_id="trade-refresh",
        evidence="first request", now_ms=NOW_MS)
    second = build_exit_only_attestation_request(
        cap, operator="operator", trade_id="trade-refresh",
        evidence="must not overwrite", now_ms=NOW_MS + 1)
    path = tmp_path / "commands" / "reconciled_exit_attestation.json"

    assert write_exit_only_attestation_request(path, first) is True
    original = path.read_text(encoding="utf-8")
    assert write_exit_only_attestation_request(path, second) is False
    assert path.read_text(encoding="utf-8") == original


def test_attestation_update_controls_are_present_without_changing_live_paper_paths():
    source = (Path(__file__).parents[2] / "ui" / "dashboard.py").read_text()

    assert "更新受限平倉對帳" in source
    assert "build_exit_only_attestation_request" in source
    assert "write_exit_only_attestation_request" in source
    assert "對帳 Trade ID（鎖定）" in source
    assert 'key="exit_only_update_trade_id"' not in source
    # Existing runtime truth branches remain distinct.
    assert 'effective_mode == "live_ready"' in source
    assert 'effective_mode == "paper_active"' in source


def test_exit_only_lifecycle_presentation_is_capability_scoped_and_ignores_bbo(
        tmp_path):
    """The lifecycle panel is an EXIT_ONLY-only, current-capability view.
    Quote-observation evidence is never a trigger or order state."""
    from ui.reconciled_exit_presentation import exit_only_lifecycle_presentation

    ctx = _context()
    events = tmp_path / "events.jsonl"
    events.write_text("\n".join(json.dumps(row) for row in (
        {"event": "EXIT_ONLY_BBO_OBSERVED", "ts": "09:00:00",
         "reconciliation_id": "reconcile-1"},
        {"event": "POLICY_J_TRIGGERED", "ts": "09:01:00",
         "reconciliation_id": "reconcile-1", "action": "COMBINED_EXIT",
         "leg_role": "BOTH", "reason": "POLICY_J_GIVEBACK"},
        {"event": "ORDER_INTENT_BLOCKED", "ts": "09:02:00",
         "action": "RELEASE_NEAR", "reason": "BBO_STALE",
         "bbo_input_v2": {"reconciliation_id": "reconcile-1"}},
        {"event": "ORDER_SUBMITTED", "ts": "09:03:00",
         "reconciliation_id": "reconcile-1", "action": "COMBINED_EXIT",
         "leg_role": "NEAR", "reason": "POLICY_J_GIVEBACK",
         "broker_order_id": "broker-1", "order_id": "local-1"},
        {"event": "ORDER_FILLED", "ts": "09:04:00",
         "reconciliation_id": "reconcile-1", "action": "COMBINED_EXIT",
         "leg_role": "NEAR", "filled_qty": 2, "fill_price": 45001.0},
        # Wrong reconciliation is legacy evidence and must not leak in.
        {"event": "ORDER_REJECTED_LOCAL", "ts": "09:05:00",
         "reconciliation_id": "old-rid", "reason": "old"},
    )) + "\n", encoding="utf-8")

    result = exit_only_lifecycle_presentation(ctx, events)
    assert result["mode"] == "reconciled_exit_only"
    assert result["capability"]["reconciliation_id"] == "reconcile-1"
    assert result["monitoring"]["state"] == "MONITORING"
    assert result["triggered"] == {
        "state": "TRIGGERED", "timestamp": "09:01:00",
        "action": "COMBINED_EXIT", "leg": "BOTH",
        "reason": "POLICY_J_GIVEBACK"}
    assert result["blocked"]["state"] == "BLOCKED"
    assert result["blocked"]["reason"] == "BBO_STALE"
    assert result["submitted"]["broker_order_id"] == "broker-1"
    assert result["terminal"] == {
        "state": "FILLED", "timestamp": "09:04:00",
        "action": "COMBINED_EXIT", "leg": "NEAR", "reason": None,
        "fill_qty": 2, "fill_price": 45001.0}


def test_exit_only_lifecycle_presentation_is_na_when_data_is_missing(tmp_path):
    from ui.reconciled_exit_presentation import exit_only_lifecycle_presentation

    result = exit_only_lifecycle_presentation(_context(), tmp_path / "none")
    assert result["monitoring"]["state"] == "MONITORING"
    assert result["triggered"] is None
    assert result["blocked"] is None
    assert result["submitted"] is None
    assert result["terminal"] is None
    assert exit_only_lifecycle_presentation(
        {"effective_mode": "paper_active"}, tmp_path / "none") is None


def test_dashboard_renders_exit_only_lifecycle_without_live_paper_fallback():
    source = (Path(__file__).parents[2] / "ui" / "dashboard.py").read_text()

    assert "MTS 受限平倉生命週期" in source
    assert "MONITORING" in source
    assert "TRIGGERED" in source
    assert "BLOCKED" in source
    assert "SUBMITTED" in source
    assert "FILLED / CANCELLED / REJECTED / TIMEOUT" in source
    assert "exit_only_lifecycle_presentation" in source
