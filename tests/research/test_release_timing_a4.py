#!/usr/bin/env python3
"""Release Timing / Reversal Controller A4(theta) — RED contract tests.

RESEARCH ONLY. A committed skeletal package (scripts/research/release_timing_a4/)
exposes the API; every contract test COLLECTS and FAILS independently on its
intended assertion (NotImplementedError). Reuses phase_transition_replay
stream/clone/tiers contracts — no ad hoc data loading.
"""

from scripts.research.release_timing_a4 import (  # noqa: F401
    branches, breach, decision, extrema, families, reports, reversal,
    state_machine, tiers)


# ── state machine / hypothesis ──────────────────────────────────────────────

def test_breach_arms_state_not_release():
    # A4(theta) never executes a release — a breach only ARMS
    state = state_machine.transition(state="NORMAL", event={"breach": True})
    assert state == "RELEASE_ARMED", state


def test_decision_outputs_r0_r1_r2_r3():
    outcome = decision.decide(
        theta={}, state="RELEASE_ARMED", extrema={}, params={})
    assert outcome in ("R0", "R1", "R2", "R3"), outcome


def test_safety_escapes_terminate_armed():
    for cause in ("combined_loss_floor", "max_adverse_excursion",
                  "max_wait", "quote_data_quality", "lifecycle_pending"):
        assert state_machine.safety_escape(cause) is True, cause


def test_safety_escape_terminal_r3_must_not_continue():
    # A4 v2 (c): a safety escape is a TERMINAL decision — after it fires,
    # R3 must never continue
    outcome = state_machine.safety_escape("max_wait")
    assert outcome in (True, "TERMINATED", "EXIT_ARMED"), outcome


def test_pending_conflict_escapes():
    # lifecycle/pending conflict is a mandatory safety escape
    assert state_machine.safety_escape("lifecycle_pending") is True


# ── breach snapshot / clone / causality ─────────────────────────────────────

def test_breach_snapshot_fields():
    snap = breach.breach_snapshot(
        event={"ts": 1}, state_clone_hash="h", config_version="v1")
    required = {"ts", "loss_leg_pnl", "combined_net", "price", "spread",
                "z", "atr", "state_clone_hash", "event_seq", "config_version"}
    assert required <= set(snap or {}), \
        f"breach snapshot missing: {required - set(snap or {})}"


def test_pre_breach_clone_completeness():
    # A4 v2 (a): clone point = the event BEFORE the breach; schema must
    # cover peak/guard warmup+armed/ATR/reference/pending candidate+orders/
    # quote freshness/controller/lifecycle/cooldown/strategy generation;
    # non-reconstructible state -> NOT_AVAILABLE
    schema = breach.clone_schema_version()
    required = {"positions", "policy_peak", "guard_warmup", "guard_armed",
                "atr", "reference_prices", "pending_candidates",
                "pending_orders", "quote_freshness", "controller",
                "lifecycle", "cooldown", "strategy_generation",
                "config_version"}
    assert required <= set(schema or {}), \
        f"clone schema missing: {required - set(schema or {})}"
    assert breach.clone_point_before_breach(event_seq=10) is not None


def test_no_lookahead_extrema():
    # running extrema update only as events arrive; consulting future
    # events must be rejected
    assert extrema.no_future_selection(extrema={}, future_events=[1]) is False


def test_running_extrema_causal_update():
    ext = extrema.update_extrema(
        extrema={"worst_combined": -10.0}, event={"combined_net": -25.0})
    assert ext is not None


# ── reversal detection ──────────────────────────────────────────────────────

def test_reversal_trigger_causal():
    trig = reversal.reversal_trigger(
        state={"armed": True}, event={"z": 2.5})
    assert trig is not None


# ── pre-registered families / no ex-post selection ──────────────────────────

def test_families_pre_registered_grids():
    assert families.a4_leg_grid([30, 60, 120]) is not None
    assert families.a4_combined_grid([50, 100, 200]) is not None
    assert families.a4_spread_z_family(
        z_reversal=2.0, velocity=True, acceleration=True) is not None


def test_theta_sweep_no_selection():
    # A4 v2 (d)+(g): thetas pre-registered per metric with units/rationale;
    # 0/-100/-200 are NESTED sensitivity, never best-threshold selection
    reg = families.theta_registry()
    assert reg is not None
    plan = families.sweep_plan(
        single_factor=["z", "leg_recovery", "combined_recovery"],
        factorial_subset={"z_x_leg": 3})
    assert plan is not None


def test_decision_rule_no_future_information():
    # A4 v2 (e): R0-R3 decisions must not use future info — the forward
    # outcome must be distinguishable from a deployable decision rule
    assert decision.forward_outcome_separate(
        decision_rule={"action": "R0"}, forward_outcome={}) is not None


# ── R3 deterministic bounded progression ────────────────────────────────────

def test_r3_deterministic_bounded():
    # R3's next decision has a fixed next level/max wait/safety — the
    # state key must be deterministic, no hindsight/combinatorial paths
    k1 = branches.branch_state_key(level=1, event_seq=42)
    k2 = branches.branch_state_key(level=1, event_seq=42)
    assert k1 == k2, "branch state key must be deterministic"
    nxt = branches.next_decision_level(level=1, max_wait=300, safety="floor")
    assert nxt is not None


def test_all_branches_share_one_immutable_stream():
    # A4 v2 (b): the immutable stream manifest must carry the full ordering
    # identity; all four branches consume the SAME stream (no per-branch
    # derived bars)
    events = stream.ordered_stream(
        events=[{"exchange_ts": 1}, {"exchange_ts": 2}],
        clock_contract="immutable-global")
    assert events is not None


# ── execution-quality tiers / config resolution ─────────────────────────────

def test_evidence_tiers_exclusive():
    # EXECUTABLE_BBO / BOUNDED_PROXY / MARK_PROXY / NOT_AVAILABLE —
    # never claim executable without BBO
    tier = tiers.evidence_tier(
        quotes={}, decision_ts=1, staleness_bounds={"max_age_s": 30})
    assert tier in ("EXECUTABLE_BBO", "BOUNDED_PROXY", "MARK_PROXY",
                    "NOT_AVAILABLE"), tier


def test_never_claim_executable_without_bbo():
    assert tiers.never_claim_executable_without_bbo(
        tier="BOUNDED_PROXY", has_bbo=False) is False


def test_params_resolve_from_deployed_config_per_event():
    # thresholds resolve from deployed config per event; missing → NOT_AVAILABLE
    params = decision.params_from_config(config={}, event={})
    assert params is not None


# ── reports / metric attribution ────────────────────────────────────────────

def test_metric_attribution_separate():
    # absolute PnL / pairwise deltas / matrix preserved; attribution is a
    # separate report, never mixed into winner selection
    assert reports.paired_delta_vs_immediate(arm={}, immediate={}) is not None
    assert reports.recovery_rate(actions=[], outcomes=[]) is not None
    assert reports.bad_execution_reduction(before={}, after={}) is not None
    assert reports.tail_mae_cost_of_waiting(arm={}, wait_horizons=[30, 120, 300]) is not None


def test_robustness_reports():
    assert reports.metric_stability(metrics={}, resamples=100) is not None
    assert reports.outlier_leave_one_out(metrics={}) is not None
    assert reports.regime_breakdown(
        metrics={}, sessions=[], vol_bins=[], z_bins=[]) is not None


# ── reuse contract: phase_transition_replay contracts ───────────────────────

def test_reuses_replay_contracts_not_ad_hoc():
    # the tiers module must wire the replay execution contract
    from scripts.research.phase_transition_replay import execution as rex
    from scripts.research.release_timing_a4 import tiers as a4_tiers
    assert a4_tiers._replay_execution is rex, \
        "A4 must reuse the replay execution contract (no ad hoc loading)"


def test_reports_reuse_replay_classify_contract():
    # A4 v2 (f): reports reuse phase-transition's four absolute Y, six
    # pairwise deltas, interval dominance and evidence-first classifier
    from scripts.research.phase_transition_replay import classify as rclassify
    from scripts.research.release_timing_a4 import reports as a4_reports
    assert a4_reports._replay_classify is rclassify, \
        "A4 reports must reuse the replay classifier (no ad hoc metrics)"
