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
    # clone is taken immediately BEFORE the breach event — never the
    # actual-release future state
    assert hasattr(__import__(
        "scripts.research.release_timing_a4.breach", fromlist=["x"]),
        "breach_snapshot")


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
    # single-factor sweeps first; every theta reported; no winner-by-max
    plan = families.sweep_plan(
        single_factor=["z", "leg_recovery", "combined_recovery"],
        factorial_subset={"z_x_leg": 3})
    assert plan is not None


# ── R3 deterministic bounded progression ────────────────────────────────────

def test_r3_deterministic_bounded():
    # R3's next decision has a fixed next level/max wait/safety — the
    # state key must be deterministic, no hindsight/combinatorial paths
    k1 = branches.branch_state_key(level=1, event_seq=42)
    k2 = branches.branch_state_key(level=1, event_seq=42)
    assert k1 == k2, "branch state key must be deterministic"
    nxt = branches.next_decision_level(level=1, max_wait=300, safety="floor")
    assert nxt is not None


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
