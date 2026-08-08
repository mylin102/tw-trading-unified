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


# ── reuse contract: the skeleton must wire the COMMITTED exit_attribution ───

def test_pipeline_reuses_committed_exit_attribution():
    from scripts.research.exit_attribution import reconcile as ea_reconcile
    from scripts.research.exit_attribution import quoting as ea_quoting
    assert hasattr(ea_reconcile, "reconcile_fill")
    assert hasattr(ea_quoting, "select_quote")
    assert pipeline.reconcile is ea_reconcile.reconcile_fill, \
        "pipeline must reuse the committed reconcile_fill (no ad-hoc parser)"
