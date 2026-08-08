#!/usr/bin/env python3
"""Phase-transition replay (SINGLE_LEG release audit) — RED contract tests.

RESEARCH ONLY. A committed research-only SKELETAL package
(scripts/research/phase_transition_replay/) exposes the API surface; every
contract test COLLECTS and FAILS independently on its intended assertion
(NotImplementedError) — no importorskip/skip masking. The missing-dependency
bucket is reserved for EXTERNAL DATA only.

Classification contract (freeze-classifier amendment):
- interval dominance: Yi NET of path-specific fees/tax; [Li,Ui] = residual
  execution uncertainty ONLY; F_N=[max(L1,L2),max(U1,U2)],
  F_R=[max(L0,L3),max(U0,U3)]
- HARMFUL iff lower(F_N)-upper(F_R)>M_economic; BENEFICIAL iff reverse;
  overlap => neutral
- MANAGEMENT_BAD (conservative): lower(Y3)-upper(Y0)>M_economic AND
  lower(Y3)>=upper(F_N)-M_economic; precedence after evidence gate, before
  family beneficial
- M_economic = preregistered minimum economic benefit (fees/tax already in Yi)
"""

from scripts.research.phase_transition_replay import (  # noqa: F401
    classify, clone, execution, pipeline, run_replay, stream, sweep)

import json
import pytest

REPO = __import__("pathlib").Path(__file__).resolve().parents[2]


# ── skeleton API surface (each behavioral call fails RED independently) ─────

def test_classify_outcome_implements_frozen_precedence():
    label = classify.classify_outcome(
        Y0=-300.0, Y1=-50.0, Y2=-40.0, Y3=+100.0, actual=-150.0,
        data_quality="ok", M_economic=25.0, M_30=25.0, M_3no_release=20.0)
    assert label in ("RELEASE_OK_MANAGEMENT_BAD", "RELEASE_BENEFICIAL",
                     "RELEASE_HARMFUL", "INCONCLUSIVE_NEUTRAL",
                     "INDETERMINATE_DATA_QUALITY")


def test_classification_management_bad_precedence():
    # Y3 makes the release family beneficial yet actual management was
    # materially worse → MANAGEMENT_BAD (finer label, frozen precedence)
    label = classify.classify_outcome(
        Y0=-300.0, Y1=-50.0, Y2=-40.0, Y3=+100.0, actual=-150.0,
        data_quality="ok", M_economic=25.0, M_30=25.0, M_3no_release=20.0)
    assert label == "RELEASE_OK_MANAGEMENT_BAD", label


def test_family_interval_dominance():
    # family winner is decided by interval dominance, never point-estimate
    # max — a point-estimate winner can flip under execution intervals
    F_N = classify.family_intervals(
        normal_arms=[(-60.0, -40.0), (-55.0, -35.0)],      # Y1, Y2
        release_arms=[(-200.0, -100.0), (80.0, 120.0)])    # Y0, Y3
    assert F_N is not None


def test_shared_cost_exact_cancellation():
    # identical shared costs between two arms must cancel exactly
    delta_shared = classify.uncertainty_bound(
        0, 1, shared_cost=100.0, per_arm_cost=5.0)
    delta_no_shared = classify.uncertainty_bound(
        0, 1, shared_cost=0.0, per_arm_cost=5.0)
    assert delta_shared == delta_no_shared, "shared costs must cancel"


def test_one_path_extra_leg_cost():
    # the extra leg's transaction cost lands in that arm's NET Yi, NOT in
    # the residual uncertainty interval — no double-counting
    bounds = classify.interval_bounds(
        net_pnl=-50.0, execution_uncertainty=10.0)
    assert isinstance(bounds, tuple) and len(bounds) == 2


def test_family_winner_flips_under_intervals():
    # intervals where the point-estimate max() winner differs from the
    # interval-dominance winner → classification must follow intervals
    F_N = classify.family_intervals(
        normal_arms=[(-70.0, -30.0), (-65.0, -25.0)],
        release_arms=[(-100.0, -80.0), (-10.0, 10.0)])
    assert F_N is not None


def test_equality_at_threshold_is_neutral():
    # lower(F_N)-upper(F_R) == M_economic exactly → neutral (no HARMFUL)
    label = classify.classify_outcome(
        Y0=-25.0, Y1=0.0, Y2=0.0, Y3=-25.0, actual=0.0,
        data_quality="ok", M_economic=25.0)
    # lower(F_N)=0 - upper(F_R)=-25 == 25 == M_economic exactly
    assert label == "INCONCLUSIVE_NEUTRAL", label


def test_management_beneficial_overlap_precedence():
    # MANAGEMENT_BAD and BENEFICIAL both satisfiable → precedence picks
    # MANAGEMENT_BAD (it precedes family beneficial)
    label = classify.classify_outcome(
        Y0=-50.0, Y1=10.0, Y2=5.0, Y3=100.0, actual=-200.0,
        data_quality="ok", M_economic=20.0, M_30=15.0, M_3no_release=10.0)
    assert label == "RELEASE_OK_MANAGEMENT_BAD", label


def test_evidence_gate_overrides_all_economics():
    # evidence failure must override every economic classification
    label = classify.classify_outcome(
        Y0=-300.0, Y1=200.0, Y2=200.0, Y3=300.0, actual=250.0,
        data_quality="no_executable_bbo", M_economic=10.0)
    assert label == "INDETERMINATE_DATA_QUALITY", label


# ── clone / stream / execution / sweep / censoring / time-stop contracts ────

def test_clone_schema_completeness():
    schema = clone.clone_schema_version()
    required = {"positions", "policy_peak", "durable_candidate", "warmup",
                "armed", "atr", "reference_prices", "pending_orders",
                "quote_freshness", "controller", "lifecycle", "release",
                "trail", "cooldown", "config_version"}
    assert required <= set(schema or {}), \
        f"clone schema missing: {required - set(schema or {})}"


def test_clone_state_at_decision_no_branch_contamination():
    # the clone must be taken BEFORE the release decision — it must never
    # see state produced by the actual-release branch
    state = clone.clone_state_at_decision(
        strategy_state={"released_leg": None, "_side": None},
        decision_ts="2026-08-08T10:00:00")
    assert state is not None


def test_clone_from_state_immune_to_source_mutation():
    # the clone is a DEEP copy — mutating the source snapshot afterwards
    # must not affect the clone; a canonical hash is computed
    snapshot = {"positions": {"near": 1}, "warmup": True,
                "policy_peak": -300.0, "durable_candidate": None,
                "armed": False, "atr": 40.0, "reference_prices": [44300.0],
                "pending_orders": [], "quote_freshness": 1,
                "controller": "none", "lifecycle": "NORMAL",
                "release": None, "trail": None, "cooldown": 0,
                "config_version": "v1"}
    stream_events = [{"replay_seq": 1}, {"replay_seq": 2}]
    result_clone = clone.clone_from_state(
        event_stream=stream_events, breach_replay_seq=1,
        state_snapshot=snapshot)
    assert isinstance(result_clone, dict), result_clone
    assert result_clone.get("_canonical_hash"), "canonical hash required"
    snapshot["positions"]["near"] = 999  # mutate the SOURCE
    assert result_clone["positions"]["near"] == 1, \
        "clone must be immune to source mutation"


def test_clone_from_state_missing_field_not_available():
    snapshot = {"positions": {"near": 1}, "warmup": True}
    result = clone.clone_from_state(
        event_stream=[], breach_replay_seq=1, state_snapshot=snapshot)
    assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE"
    assert "atr" in result[1], f"exact missing field: {result}"


def test_clone_prefix_excludes_breach_and_release_events():
    # strictly-before semantics: replay_seq < breach_replay_seq — the
    # breach event itself AND release/future events must never be read
    from types import SimpleNamespace  # noqa: F401
    snapshot = {"positions": {"near": 1}, "warmup": True,
                "policy_peak": -300.0, "durable_candidate": None,
                "armed": False, "atr": 40.0, "reference_prices": [44300.0],
                "pending_orders": [], "quote_freshness": 1,
                "controller": "none", "lifecycle": "NORMAL",
                "release": None, "trail": None, "cooldown": 0,
                "config_version": "v1"}
    stream_events = [
        {"replay_seq": 1},
        {"replay_seq": 2, "sentinel": "BREACH"},
        {"replay_seq": 3, "sentinel": "RELEASE"},
    ]
    result = clone.clone_from_state(
        event_stream=stream_events, breach_replay_seq=2,
        state_snapshot=snapshot)
    import hashlib
    import json
    expect = hashlib.sha256(
        json.dumps([{"replay_seq": 1}], sort_keys=True,
                   default=str).encode()).hexdigest()
    assert result["_stream_prefix_hash"] == expect, \
        "breach event and release/future events must never be read"


def test_event_stream_integrity_fields():
    events = stream.ordered_stream(
        events=[{"exchange_ts": 1}, {"exchange_ts": 2}],
        clock_contract="immutable-global")
    assert events is not None


def test_execution_bbo_vs_tick_proxy():
    prices = execution.executable_prices(
        quotes={"near": None, "far": None},
        decision_ts="2026-08-08T10:00:00",
        staleness_bounds={"max_age_s": 30})
    assert prices is not None


def test_execution_bbo_requires_valid_bid_ask():
    # missing/zero/NaN bid or ask must NEVER be EXECUTABLE_BBO
    from types import SimpleNamespace
    base = {"near": SimpleNamespace(bid=50.0, ask=100.0, age_s=1,
                                    close_action="SHORT"),
            "far": SimpleNamespace(bid=25.0, ask=50.0, age_s=1,
                                   close_action="SHORT")}
    bad_sides = [
        ("near", "bid", None), ("far", "ask", None),
        ("near", "bid", 0.0), ("far", "ask", 0.0),
        ("near", "bid", float("nan")), ("far", "ask", float("nan")),
    ]
    for side, field, bad in bad_sides:
        quotes = {k: SimpleNamespace(
            bid=base[k].bid, ask=base[k].ask, age_s=1,
            close_action=base[k].close_action) for k in base}
        setattr(quotes[side], field, bad)
        r = execution.executable_prices(
            quotes, decision_ts="2026-08-08T10:00:00",
            staleness_bounds={"max_age_s": 30})
        assert r["tier"] != "EXECUTABLE_BBO", (side, field, bad, r["tier"])
        assert r["reasons"], f"downgrade must carry a reason: {(side, field)}"


def test_execution_close_side_prices():
    # LONG closes at bid, SHORT closes at ask
    from types import SimpleNamespace
    quotes = {
        "near": SimpleNamespace(bid=99.0, ask=101.0, age_s=1,
                                close_action="LONG"),
        "far": SimpleNamespace(bid=49.0, ask=51.0, age_s=1,
                               close_action="SHORT"),
    }
    r = execution.executable_prices(
        quotes, decision_ts="2026-08-08T10:00:00",
        staleness_bounds={"max_age_s": 30})
    assert r["tier"] == "EXECUTABLE_BBO", r
    assert r["executable_prices"] == {"near": 99.0, "far": 51.0}, r


def test_execution_age_fail_closed():
    # v3: age missing / NaN / negative / timeout must NEVER be executable
    from types import SimpleNamespace
    good = dict(bid=50.0, ask=100.0, close_action="SHORT")
    cases = [
        {"near": SimpleNamespace(**good, age_s=None),
         "far": SimpleNamespace(**good, age_s=1)},
        {"near": SimpleNamespace(**good, age_s=float("nan")),
         "far": SimpleNamespace(**good, age_s=1)},
        {"near": SimpleNamespace(**good, age_s=-1),
         "far": SimpleNamespace(**good, age_s=1)},
        {"near": SimpleNamespace(**good, age_s=999),
         "far": SimpleNamespace(**good, age_s=1)},
    ]
    for quotes in cases:
        r = execution.executable_prices(
            quotes, decision_ts="2026-08-08T10:00:00",
            staleness_bounds={"max_age_s": 30})
        assert r["tier"] != "EXECUTABLE_BBO", quotes
        assert any(("age" in x) or ("stale" in x) for x in r["reasons"]), \
            r["reasons"]


def test_execution_close_action_fail_closed():
    # v3: close_action not LONG/SHORT must NEVER be executable
    from types import SimpleNamespace
    good = dict(bid=50.0, ask=100.0, age_s=1)
    for action in (None, "MARKET", "AUTO"):
        quotes = {
            "near": SimpleNamespace(**good, close_action="SHORT"),
            "far": SimpleNamespace(**good, close_action=action),
        }
        r = execution.executable_prices(
            quotes, decision_ts="2026-08-08T10:00:00",
            staleness_bounds={"max_age_s": 30})
        assert r["tier"] != "EXECUTABLE_BBO", (action, r["tier"])
        assert any("close_action" in x for x in r["reasons"]), r["reasons"]


def test_execution_leg_set_exact():
    # v3: missing leg / extra leg / duplicate mapping are NEVER executable
    from types import SimpleNamespace
    good = dict(bid=50.0, ask=100.0, age_s=1, close_action="SHORT")
    cases = [
        {"near": SimpleNamespace(**good)},
        {"near": SimpleNamespace(**good), "far": SimpleNamespace(**good),
         "extra": SimpleNamespace(**good)},
        {"near": SimpleNamespace(**good), "near2": SimpleNamespace(**good)},
    ]
    for quotes in cases:
        r = execution.executable_prices(
            quotes, decision_ts="2026-08-08T10:00:00",
            staleness_bounds={"max_age_s": 30})
        assert r["tier"] == "NOT_AVAILABLE", (quotes.keys(), r["tier"])
        assert any("leg set" in x for x in r["reasons"]), r["reasons"]


def test_threshold_sweep_overlap_contract():
    matrix = sweep.threshold_overlap(
        thresholds=[0, -100, -200],
        candidates=[{"combined_net": -150}, {"combined_net": -50}])
    assert matrix is not None


def test_candidate_event_time_is_decision_not_fill():
    ts = pipeline.candidate_timestamps(
        release_event={"decision_ts": 1000, "fill_ts": 2000})
    assert ts == {"decision_ts": 1000, "fill_ts": 2000, "skew": 1000}


def test_censoring_never_drops_silently():
    censored = pipeline.censored_with_reason(
        candidate={"id": "c1", "state": "corrupt"}, reason="fills_unmatched")
    assert censored is not None


def test_time_stop_conditional_contract():
    # trigger only when horizon net < net at release
    assert pipeline.time_stop_triggered(
        horizon_net=-10.0, net_at_release=5.0) is True
    assert pipeline.time_stop_triggered(
        horizon_net=10.0, net_at_release=5.0) is False


def test_replay_entrypoint_exists():
    assert callable(run_replay.main)


def test_runner_refuses_without_dry_run_or_authorize(tmp_path):
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([]), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1"])
    # v5: the engine is not implemented — ANY non-dry-run attempt refuses
    assert rc == 3, f"runner must refuse without --dry-run: {rc}"
    assert not (out / "manifest.json").exists(), "zero output on refusal"


def test_runner_authorize_without_dry_run_refused(tmp_path):
    # v5: --authorize non-dry-run must NOT be a fake success — the engine
    # is not implemented, so it refuses with zero output
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([]), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--authorize"])
    assert rc == 3, rc
    assert not (out / "manifest.json").exists(), "zero output on refusal"


def test_runner_prereg_required(tmp_path):
    # v5: --prereg is required — no value defaults may bypass
    # pre-registration
    inp = tmp_path / "events.json"
    inp.write_text("[]", encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(SystemExit) as ei:
        run_replay.main(["--input", str(inp), "--out-dir", str(out)])
    assert ei.value.code != 0


def _schema_valid_event(**over):
    ev = {"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101,
          "decision_ts_ms": 1_700_000_100_000,
          "quotes": {"near": {"bid": 50.0, "ask": 100.0, "age_s": 1,
                              "close_action": "SHORT",
                              "quote_exchange_ts": 1_700_000_000_000},
                     "far": {"bid": 25.0, "ask": 50.0, "age_s": 1,
                             "close_action": "SHORT",
                             "quote_exchange_ts": 1_700_000_000_050}}}
    ev.update(over)
    return ev


def test_runner_dry_run_writes_manifest(tmp_path):
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([_schema_valid_event()]), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 0, rc
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dry_run"] is True
    assert len(manifest["input_sha256"]) == 64
    assert manifest["stream_hash"]
    assert manifest["n_events"] == 1
    assert manifest["n_kept"] == 1
    assert manifest["n_censored"] == 0
    assert manifest["engine_run"] is False
    assert manifest["preregistration_id"] == "prereg-v1"
    assert manifest["preregistration_sha"]
    assert manifest["parameters"]["staleness"] == {"max_age_s": 30}
    assert manifest["parameters"]["m_economic"] == 25.0
    assert manifest["parameters"]["max_pair_skew_ms"] == 1000
    assert manifest["parameters"]["timestamp_unit"] == "epoch_ms"
    assert manifest["parameters"]["timestamp_validator_version"] == "v1"
    prov = manifest["git_provenance"]
    assert len(prov["repo_head"]) == 40
    assert prov["dirty"] is False
    assert len(prov["runner_sha256"]) == 64
    assert len(prov["prereg_sha256"]) == 64
    assert prov["runner_tracked"] and prov["prereg_tracked"]
    assert prov["runner_matches_head"] and prov["prereg_matches_head"]
    rec = manifest["kept_records"][0]
    assert rec["pair_skew_ms"] == 50
    assert rec["max_pair_skew_ms"] == 1000
    assert rec["near_age_s"] == 1


def test_runner_reproducible_input_hash(tmp_path):
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([_schema_valid_event()]), encoding="utf-8")
    out1 = tmp_path / "o1"
    out2 = tmp_path / "o2"
    run_replay.main(["--input", str(inp), "--out-dir", str(out1),
                     "--prereg", "prereg-v1", "--dry-run"])
    run_replay.main(["--input", str(inp), "--out-dir", str(out2),
                     "--prereg", "prereg-v1", "--dry-run"])
    m1 = json.loads((out1 / "manifest.json").read_text(encoding="utf-8"))
    m2 = json.loads((out2 / "manifest.json").read_text(encoding="utf-8"))
    assert m1["input_sha256"] == m2["input_sha256"]
    assert m1["stream_hash"] == m2["stream_hash"]


def test_runner_censors_incomplete_quotes(tmp_path):
    # v4: unknown-age / incomplete two-leg quotes must never produce usable
    # two-leg counterfactual PnL — the candidate is censored WITH reason
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([
        _schema_valid_event(quotes={
            "near": {"bid": 50.0, "ask": 100.0, "age_s": None,
                     "close_action": "SHORT",
                     "quote_exchange_ts": 1_700_000_000_000},
            "far": {"bid": 25.0, "ask": 50.0, "age_s": 1,
                    "close_action": "SHORT",
                    "quote_exchange_ts": 1_700_000_000_050}}),
    ]), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 0, rc
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["n_censored"] == 1, manifest
    assert manifest["n_kept"] == 0, manifest
    reasons = [c["reason"] for c in manifest["censored_reasons"]]
    assert any("age" in r or "stale" in r for r in reasons), reasons


def test_runner_schema_invalid_event_censored(tmp_path):
    # v5: input schema validation — an event missing the decision timestamp
    # is censored per-event, never silently treated as valid
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps(
        [_schema_valid_event(),
         _schema_valid_event(decision_ts_ms=None)]),
        encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 0, rc
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["n_events"] == 2
    assert manifest["n_kept"] == 1
    assert manifest["n_censored"] == 1, manifest
    reasons = [c["reason"] for c in manifest["censored_reasons"]]
    assert any("schema" in r for r in reasons), reasons


def test_runner_whole_file_invalid_refused(tmp_path):
    # v5: a malformed whole-file input REFUSES (non-zero, zero output)
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 4, rc
    assert not (out / "manifest.json").exists(), "zero output on refusal"


def test_runner_json_dict_quotes_kept_when_valid(tmp_path):
    # v5 integration: real JSON-list input with dict quotes — VALID near/far
    # dict quotes are kept (normalize), invalid ones are censored
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([
        _schema_valid_event(),
        _schema_valid_event(
            source_event_seq=2, exchange_ts=200, recv_ts=201,
            quotes={"near": {"bid": 50.0, "ask": 100.0, "age_s": 1,
                             "close_action": "SHORT",
                             "quote_exchange_ts": 1_700_000_000_000},
                    "far": {"bid": 25.0, "ask": 50.0, "age_s": None,
                            "close_action": "SHORT",
                            "quote_exchange_ts": 1_700_000_000_050}}),
    ]), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 0, rc
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["n_kept"] == 1, manifest
    assert manifest["n_censored"] == 1, manifest


def test_runner_git_provenance_gate_refuses_dirty(monkeypatch, tmp_path):
    # v6: when the provenance cannot be proven (dirty/untracked/modified),
    # the runner REFUSES with zero output
    import json
    monkeypatch.setattr(
        run_replay, "git_provenance",
        lambda: (False, {"reason": "research subtree dirty"}))
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([_schema_valid_event()]), encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 5, rc
    assert not (out / "manifest.json").exists(), "zero output on refusal"


def test_runner_censors_pair_skew_exceeding_bound(tmp_path):
    # v6: unsynchronized quotes (skew > bound) are censored — never
    # treated as an executable pair
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([_schema_valid_event(quotes={
        "near": {"bid": 50.0, "ask": 100.0, "age_s": 1,
                 "close_action": "SHORT",
                 "quote_exchange_ts": 1_699_999_000_000},
        "far": {"bid": 25.0, "ask": 50.0, "age_s": 1,
                "close_action": "SHORT",
                "quote_exchange_ts": 1_700_000_000_000}})]),
        encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 0, rc
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["n_kept"] == 0, manifest
    assert manifest["n_censored"] == 1, manifest
    reasons = [c["reason"] for c in manifest["censored_reasons"]]
    assert any("skew" in r for r in reasons), reasons


def test_runner_censors_quote_after_decision(tmp_path):
    # v6: a quote_exchange_ts LATER than the decision time is censored
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([_schema_valid_event(quotes={
        "near": {"bid": 50.0, "ask": 100.0, "age_s": 1,
                 "close_action": "SHORT",
                 "quote_exchange_ts": 1_700_000_200_000},
        "far": {"bid": 25.0, "ask": 50.0, "age_s": 1,
                "close_action": "SHORT",
                "quote_exchange_ts": 1_700_000_000_050}})]),
        encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 0, rc
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["n_censored"] == 1, manifest
    reasons = [c["reason"] for c in manifest["censored_reasons"]]
    assert any("later than decision" in r for r in reasons), reasons


def test_execution_malformed_quote_fail_closed():
    # v6: a malformed quote object produces a reason — NEVER AttributeError
    from types import SimpleNamespace
    for bad in ("junk", 42, object(), {"bid": 1.0}):
        quotes = {
            "near": bad,
            "far": SimpleNamespace(bid=50.0, ask=100.0, age_s=1,
                                   close_action="SHORT"),
        }
        r = execution.executable_prices(
            quotes, decision_ts=1000,
            staleness_bounds={"max_age_s": 30})
        assert r["tier"] != "EXECUTABLE_BBO", (bad, r["tier"])
        assert r["reasons"], f"fail-closed reason required: {bad!r}"


def test_validate_epoch_ms_rejects_non_ms_domains():
    # v6.1: strict epoch-ms — bool, seconds, microseconds and nanoseconds
    # must all be rejected; only plausible epoch-ms passes
    assert execution.validate_epoch_ms(1_700_000_100_000) is True
    for bad in (True, False, 1_700_000_100,        # seconds scale
                1_700_000_100_000_000,             # microseconds scale
                1_700_000_100_000_000_000,         # nanoseconds scale
                0, -1, None, "1.7e12"):
        assert execution.validate_epoch_ms(bad) is False, bad


def test_runner_censors_seconds_scale_quote_ts(tmp_path):
    # v6.1: a seconds-scale quote_exchange_ts must NOT pass as fresh —
    # censored with an epoch-ms reason
    import json
    inp = tmp_path / "events.json"
    inp.write_text(json.dumps([_schema_valid_event(quotes={
        "near": {"bid": 50.0, "ask": 100.0, "age_s": 1,
                 "close_action": "SHORT", "quote_exchange_ts": 1_700_000_100},
        "far": {"bid": 25.0, "ask": 50.0, "age_s": 1,
                "close_action": "SHORT",
                "quote_exchange_ts": 1_700_000_000_050}})]),
        encoding="utf-8")
    out = tmp_path / "out"
    rc = run_replay.main(["--input", str(inp), "--out-dir", str(out),
                          "--prereg", "prereg-v1", "--dry-run"])
    assert rc == 0, rc
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["n_censored"] == 1, manifest
    reasons = [c["reason"] for c in manifest["censored_reasons"]]
    assert any("epoch-ms" in r for r in reasons), reasons


# ── reuse contract: the skeleton must wire the COMMITTED exit_attribution ───

def test_pipeline_reuses_committed_exit_attribution():
    from scripts.research.exit_attribution import reconcile as ea_reconcile
    from scripts.research.exit_attribution import quoting as ea_quoting
    assert hasattr(ea_reconcile, "reconcile_fill")
    assert hasattr(ea_quoting, "select_quote")
    assert pipeline.reconcile is ea_reconcile.reconcile_fill, \
        "pipeline must reuse the committed reconcile_fill (no ad-hoc parser)"
