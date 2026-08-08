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
from scripts.research.phase_transition_replay import stream  # shared contract


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
        td = state_machine.safety_escape(cause)
        assert td.__class__.__name__ == "TerminalDecision", (cause, td)
        assert td.cause == cause and td.terminal is True, (cause, td)


def test_safety_escape_terminal_r3_must_not_continue():
    # A4 v2.2 (2): a safety escape must return a typed TerminalDecision /
    # raise a specific exception; after it fires there is ZERO R3
    # transition and ZERO order candidate — not merely "!= R3"
    td = state_machine.safety_escape("max_wait")
    assert td.__class__.__name__ == "TerminalDecision", td
    assert td.cause == "max_wait" and td.terminal is True
    with pytest.raises(Exception) as ei:
        decision.decide(theta={}, state=td, extrema={}, params={})
    assert type(ei.value).__name__ == "TerminalDecision" or \
        "terminal" in type(ei.value).__name__.lower(), ei.value


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
    # A4 v2.2 (1): the clone API must return a TYPED NOT_AVAILABLE result
    # naming the exact missing field — no vacuous fallback
    schema = breach.clone_schema_version()
    required = {"positions", "policy_peak", "guard_warmup", "guard_armed",
                "atr", "reference_prices", "pending_candidates",
                "pending_orders", "quote_freshness", "controller",
                "lifecycle", "cooldown", "strategy_generation",
                "config_version"}
    assert required <= set(schema or {}), \
        f"clone schema missing: {required - set(schema or {})}"
    clone = breach.clone_point_before_breach(event_seq=10)
    assert clone is not None
    for field in required:
        result = breach.clone_point_before_breach(
            event_seq=10, missing_fields={field})
        assert isinstance(result, tuple) and result[0] == "NOT_AVAILABLE", \
            f"missing {field} must yield typed NOT_AVAILABLE: {result}"
        assert field in result[1], \
            f"NOT_AVAILABLE must name the exact missing field: {result}"


def test_clone_immune_to_actual_branch_mutation():
    # A4 v2.2 (2b): mutate the ACTUAL-release branch state — the A4 clone
    # (taken before the breach) must remain unchanged
    clone_a = breach.clone_point_before_breach(event_seq=10)
    clone_b = breach.clone_point_before_breach(
        event_seq=10, actual_branch_mutated={"released_leg": "near"})
    assert clone_a == clone_b, "A4 clone must not see actual-release state"


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
    # A4 v2.2 (4): every grid value FINITE + explicit unit/rationale per
    # metric; NO default-true — all nested threshold result rows asserted
    reg = families.theta_registry()
    assert reg, "registry must be non-empty"
    for metric, spec in (reg or {}).items():
        assert "unit" in spec and "rationale" in spec and "grid" in spec, \
            f"theta spec incomplete for {metric}: {spec}"
        assert spec["unit"], f"explicit unit required for {metric}"
        assert spec["rationale"], f"explicit rationale required for {metric}"
        grid = spec["grid"]
        assert isinstance(grid, (list, tuple)) and grid, \
            f"finite preregistered grid required for {metric}"
        assert all(isinstance(v, (int, float)) and v == v
                   and v != float("inf") and v != float("-inf")
                   for v in grid), f"all grid values must be finite: {metric}"
    plan = families.sweep_plan(
        single_factor=["z", "leg_recovery", "combined_recovery"],
        factorial_subset={"z_x_leg": 3})
    assert "reports_all_thetas" in plan, \
        "reports_all_thetas must be explicit — no default-true"
    assert plan["reports_all_thetas"] is True
    rows = plan.get("nested_threshold_rows")
    assert rows, "all nested threshold result rows must be asserted"
    assert isinstance(rows, (list, tuple)) and len(rows) >= 3, \
        "0/-100/-200 nested rows must all be present"


def test_decision_rule_no_future_information():
    # A4 v2.2 (5): a FUTURE sentinel is embedded in the immutable stream
    # AFTER the decision time; the decision trace/read-set must EXCLUDE
    # its replay_seq while the forward evaluator consumes it
    events, stream_hash, clock = stream.ordered_stream(
        events=[{"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101},
                {"source_event_seq": 2, "exchange_ts": 102, "recv_ts": 103},
                {"source_event_seq": 3, "exchange_ts": 200, "recv_ts": 201,
                 "sentinel": "REVERSAL"}],
        clock_contract="immutable-global")
    decision_ts = events[0]["replay_seq"]
    future_seqs = {e["replay_seq"] for e in events
                   if e["replay_seq"] > decision_ts}
    assert future_seqs, "the stream must contain future events"
    trace = decision.decide_trace(theta={}, state="RELEASE_ARMED",
                                  extrema={}, params={}, events=events)
    read_set = set(trace.get("read_replay_seqs", []))
    assert read_set, "decision trace must report its read-set"
    assert not (read_set & future_seqs), \
        f"decision read-set must exclude future replay_seqs: {read_set}"
    assert max(read_set) <= decision_ts
    assert decision.forward_evaluator(
        events=events, decision_ts=decision_ts) is not None


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
    # A4 v2.2 (3): four branch CONSUMERS share the SAME stream object
    # identity/hash with identical derived bars. Each consumer accepts the
    # SAME events stream and reports its input reference — the returned
    # input_id must BE the events object's id (not merely equal bar values)
    events, stream_hash, clock = stream.ordered_stream(
        events=[{"source_event_seq": 1, "exchange_ts": 100, "recv_ts": 101},
                {"source_event_seq": 2, "exchange_ts": 102, "recv_ts": 103}],
        clock_contract="immutable-global")
    for ev in events:
        assert {"source_event_seq", "exchange_ts", "recv_ts", "replay_seq"} \
            <= set(ev), f"manifest fields missing: {ev}"
    assert stream_hash is not None
    consumers = []
    for i in range(4):
        consumers.append(branches.derived_bars(events))
    for i, c in enumerate(consumers):
        assert c["input_id"] == id(events), \
            f"branch {i} must consume THE SAME events object"
        assert c["stream_hash"] == stream_hash, \
            f"branch {i} must report the same stream hash"
    assert all(c["bars"] == consumers[0]["bars"] for c in consumers), \
        "identical derived-bar sequences across branches"
    assert clock == "immutable-global"


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
    # A4 v2.2 (6): EXACT Y0..Y3 values, EXACTLY six named deltas with
    # expected values, evidence-gate precedence and the ACTUAL interval
    # classification result — not non-None/hasattr
    from scripts.research.release_timing_a4 import reports as a4_reports
    matrix = a4_reports.arm_matrix(arms={
        "Y0": -300.0, "Y1": -50.0, "Y2": -40.0, "Y3": 100.0})
    assert matrix["absolute_Y"] == \
        {"Y0": -300.0, "Y1": -50.0, "Y2": -40.0, "Y3": 100.0}, \
        f"exact four absolute Y required: {matrix.get('absolute_Y')}"
    deltas = matrix["pairwise_deltas"]
    assert set(deltas) == {"d01", "d02", "d03", "d12", "d13", "d23"}, \
        f"exactly six named deltas required: {set(deltas)}"
    assert deltas["d02"] == pytest.approx(-260.0), deltas["d02"]
    assert matrix["evidence_gate_precedence"] is True, \
        "evidence gate must precede every economic classification"
    label = matrix["interval_classification"]
    assert label in ("INDETERMINATE_DATA_QUALITY", "RELEASE_HARMFUL",
                     "RELEASE_BENEFICIAL", "RELEASE_OK_MANAGEMENT_BAD",
                     "INCONCLUSIVE_NEUTRAL"), label
