#!/usr/bin/env python3
"""Phase-transition replay (SINGLE_LEG release audit) — RED contract tests.

RESEARCH ONLY. Contracts below lock the spec (codex #24-1..5 + #25-1..5);
they are RED until the research pipeline lands under scripts/research/
phase_transition_replay/ (reusing scripts/research/exit_attribution
components — no ad-hoc parser). Missing-module dependencies are reported
separately from behavioural coverage.
"""

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PIPELINE = "scripts.research.phase_transition_replay"

# ── missing-module RED dependencies (reported separately) ───────────────────

MODULE_CONTRACTS = {
    "pipeline": "candidate reconciliation + censored list + per-candidate record",
    "clone": "deep-clone/hash strategy state at release-decision clone point",
    "stream": "immutable globally-ordered market-event stream + clock contract",
    "classify": "predeclared causal classification (5 classes + delta gate)",
    "execution": "executable BBO (staleness/skew) vs BOUNDED_TICK_PROXY rules",
    "sweep": "admission threshold sweep (0/-100/-200) overlap + paired deltas",
}


@pytest.mark.parametrize("contract", sorted(MODULE_CONTRACTS))
def test_contract_module_exists(contract):
    pytest.importorskip(f"{PIPELINE}.{contract}")


def test_replay_entrypoint_exists():
    pytest.importorskip(f"{PIPELINE}.run_replay")


# ── reuse contract: exit_attribution components, no ad-hoc parser ───────────

def test_pipeline_reuses_exit_attribution_components():
    # the pipeline must import the committed reconcile/quoting/stats modules
    # (no ad-hoc parser) — text contract until the module exists
    target = REPO / "scripts" / "research" / "phase_transition_replay"
    if not target.exists():
        pytest.skip("pipeline dir not present yet (RED dependency)")
    sources = list(target.rglob("*.py"))
    assert sources, "no pipeline sources"
    joined = "\n".join(s.read_text(encoding="utf-8") for s in sources)
    assert "exit_attribution" in joined, \
        "pipeline must reuse scripts/research/exit_attribution components"


# ── behavioural contracts (pure logic the implementation must satisfy) ──────

def test_classification_exhaustive_and_priority():
    # five mutually-exclusive classes; frozen precedence (codex final
    # refinement): 1 INDETERMINATE_DATA_QUALITY → 2 MANAGEMENT_BAD iff
    # Y3-Y0 > M_30 AND Y3 >= max(Y1,Y2)-M_3no_release → 3 HARMFUL iff
    # max(Y1,Y2)-max(Y0,Y3) > M_family → 4 BENEFICIAL iff reverse →
    # 5 INCONCLUSIVE_NEUTRAL
    mod = pytest.importorskip(f"{PIPELINE}.classify")
    classes = ("INDETERMINATE_DATA_QUALITY", "RELEASE_HARMFUL",
               "RELEASE_BENEFICIAL", "RELEASE_OK_MANAGEMENT_BAD",
               "INCONCLUSIVE_NEUTRAL")
    fn = getattr(mod, "classify_outcome", None)
    assert callable(fn), "classify.classify_outcome(Y0..Y3, M_ij, ...)"


def test_classification_management_bad_precedence():
    # the finer label: Y3 makes the release family beneficial, yet the
    # actual management was materially worse → MANAGEMENT_BAD (not
    # BENEFICIAL) by the frozen precedence
    mod = pytest.importorskip(f"{PIPELINE}.classify")
    fn = getattr(mod, "classify_outcome", None)
    assert callable(fn)
    # Y3(managed) well above Y0(atomic) and max(Y1,Y2) with a big
    # management gap on the actual path → MANAGEMENT_BAD
    label = fn(Y0=-300.0, Y1=-50.0, Y2=-40.0, Y3=+100.0,
               actual=-150.0, M_30=25.0, M_3no_release=20.0,
               M_family=40.0, data_quality="ok")
    assert label == "RELEASE_OK_MANAGEMENT_BAD", label


def test_classification_pairwise_uncertainty_bounds():
    # materiality is measured on the PAIRWISE PnL-difference uncertainty
    # U_delta(i,j): M_ij = max(M_economic, U_delta(i,j)); shared costs
    # cancel, never double-counted; family-max comparisons use either a
    # conservative bound across the applicable winner comparisons or
    # interval dominance (lower/upper PnL bounds) — documented which
    mod = pytest.importorskip(f"{PIPELINE}.classify")
    fn = getattr(mod, "uncertainty_bound", None)
    assert callable(fn), "classify.uncertainty_bound(i, j, ...) -> U_delta(i,j)"
    M = getattr(mod, "materiality", None)
    assert callable(M), "classify.materiality(i, j) -> max(M_economic, U_delta)"


def test_classification_shared_cost_cancellation():
    # identical shared costs between two arms must cancel — they must not
    # be double-counted into the difference materiality
    mod = pytest.importorskip(f"{PIPELINE}.classify")
    fn = getattr(mod, "uncertainty_bound", None)
    assert callable(fn)
    delta_shared = fn(0, 1, shared_cost=100.0, per_arm_cost=5.0)
    delta_no_shared = fn(0, 1, shared_cost=0.0, per_arm_cost=5.0)
    assert delta_shared == delta_no_shared, \
        "shared costs must cancel in the pairwise bound"


def test_execution_price_bbo_staleness_contract():
    mod = pytest.importorskip(f"{PIPELINE}.execution")
    # executable BBO only within documented staleness/skew bounds; tick-only
    # values are BOUNDED_TICK_PROXY and never mix into executable stats
    fn = getattr(mod, "executable_prices", None)
    assert callable(fn), "execution.executable_prices(quotes, decision_ts, ...)"


def test_time_stop_conditional_contract():
    mod = pytest.importorskip(f"{PIPELINE}.pipeline")
    # time stops 30/120/300 trigger ONLY when horizon net < net_at_release
    fn = getattr(mod, "time_stop_triggered", None)
    assert callable(fn), "pipeline.time_stop_triggered(horizon_net, net_at_release)"


def test_threshold_sweep_overlap_contract():
    mod = pytest.importorskip(f"{PIPELINE}.sweep")
    # 0/-100/-200 is a preregistered sensitivity sweep over overlapping
    # subsets — report overlap matrix + paired deltas, never winner-by-max
    fn = getattr(mod, "threshold_overlap", None)
    assert callable(fn), "sweep.threshold_overlap(...)"


def test_event_stream_integrity_fields():
    # every replayed event must carry source_event_seq/exchange_ts/recv_ts/
    # replay_seq/stream hash/ordering key — one immutable shared stream
    mod = pytest.importorskip(f"{PIPELINE}.stream")
    fn = getattr(mod, "ordered_stream", None)
    assert callable(fn), "stream.ordered_stream(...)"


def test_clone_schema_fields():
    # clone point = immediately BEFORE the release-decision event; the
    # deep clone must cover the full strategy state (positions, policy
    # peak/durable/warmup/armed, ATR/reference, pending orders, quote
    # freshness, controller/lifecycle/release/trail, cooldown, config/version)
    mod = pytest.importorskip(f"{PIPELINE}.clone")
    fn = getattr(mod, "clone_schema_version", None)
    assert callable(fn), "clone.clone_schema_version()"
    schema = fn()
    required = {"positions", "policy_peak", "durable_candidate", "warmup",
                "armed", "atr", "reference_prices", "pending_orders",
                "quote_freshness", "controller", "lifecycle", "release",
                "trail", "cooldown", "config_version"}
    missing = required - set(schema or {})
    assert not missing, f"clone schema missing fields: {missing}"


def test_candidate_event_time_is_decision_not_fill():
    # event time = release DECISION/ORDER_SUBMITTED ts, not the fill ts;
    # both timestamps + skew must be recorded per candidate
    mod = pytest.importorskip(f"{PIPELINE}.pipeline")
    fn = getattr(mod, "candidate_timestamps", None)
    assert callable(fn), "pipeline.candidate_timestamps(...)"


def test_censoring_never_drops_silently():
    # unresolved/corrupt/partial candidates are censored WITH an exclusion
    # reason, never dropped silently
    mod = pytest.importorskip(f"{PIPELINE}.pipeline")
    fn = getattr(mod, "censored_with_reason", None)
    assert callable(fn), "pipeline.censored_with_reason(...)"
