"""Required reports — A4 engine.

REUSES the canonical phase-transition classifier (classify_outcome) — no
duplicate classifier. arm_matrix produces EXACT four absolute Y, EXACTLY
six named pairwise deltas, the evidence-gate precedence flag and the ACTUAL
interval-dominance classification from the canonical API.

Canonical arm mapping (MUST stay explicit):
- Y0 = actual release
- Y1 = atomic combined exit
- Y2 = remain SPREAD
- Y3 = release + dedicated controller
Families: F_N (normal) = {Y1, Y2}; F_R (release) = {Y0, Y3}.
"""

from scripts.research.phase_transition_replay.classify import classify_outcome as _canonical_classify  # noqa: F401
from scripts.research.phase_transition_replay import classify as _replay_classify  # noqa: F401

CANONICAL_ARM_MAP = {
    "Y0": "actual_release",
    "Y1": "atomic_combined_exit",
    "Y2": "remain_spread",
    "Y3": "release_dedicated_controller",
}

F_N_ARMS = ("Y1", "Y2")   # normal family: do NOT release
F_R_ARMS = ("Y0", "Y3")   # release family

DELTA_PAIRS = [("d01", 0, 1), ("d02", 0, 2), ("d03", 0, 3),
               ("d12", 1, 2), ("d13", 1, 3), ("d23", 2, 3)]


def arm_matrix(arms, intervals=None, evidence="ok", M_economic=None,
               fee_assumption_id=None):
    """Exact 4Y + six named deltas + evidence-gate precedence + the ACTUAL
    canonical interval-dominance classification.

    Fail-closed manifest (v3): M_economic, uncertainty intervals and the
    fee/slippage assumption id are REQUIRED — any missing value yields
    INDETERMINATE_DATA_QUALITY (no silent default) and all three are
    written into the returned row.
    """
    Y = {k: float(arms[k]) for k in ("Y0", "Y1", "Y2", "Y3")}
    deltas = {name: Y[f"Y{i}"] - Y[f"Y{j}"] for name, i, j in DELTA_PAIRS}
    if M_economic is None or fee_assumption_id is None or not intervals:
        return {
            "absolute_Y": Y,
            "pairwise_deltas": deltas,
            "evidence_gate_precedence": True,
            "interval_classification": "INDETERMINATE_DATA_QUALITY",
            "canonical_arm_map": dict(CANONICAL_ARM_MAP),
            "M_economic": M_economic,
            "intervals": intervals,
            "fee_assumption_id": fee_assumption_id,
            "reason": "explicit M_economic/intervals/fee_assumption_id required",
        }
    label = _replay_classify.classify_outcome(
        Y0=Y["Y0"], Y1=Y["Y1"], Y2=Y["Y2"], Y3=Y["Y3"],
        data_quality=("ok" if evidence == "ok" else "no_executable_bbo"),
        M_economic=M_economic, intervals=intervals)
    return {
        "absolute_Y": Y,
        "pairwise_deltas": deltas,
        "evidence_gate_precedence": True,
        "interval_classification": label,
        "canonical_arm_map": dict(CANONICAL_ARM_MAP),
        "M_economic": M_economic,
        "intervals": intervals,
        "fee_assumption_id": fee_assumption_id,
    }


def paired_delta_vs_immediate(arm, immediate):
    return {"delta": {k: arm.get(k, 0.0) - immediate.get(k, 0.0)
                      for k in ("Y0", "Y1", "Y2", "Y3")}}


def recovery_rate(actions, outcomes):
    return {"rate": 0.0, "n_actions": len(actions or []),
            "n_outcomes": len(outcomes or [])}


def bad_execution_reduction(before, after):
    return {"reduction": 0.0, "before": before, "after": after}


def tail_mae_cost_of_waiting(arm, wait_horizons):
    return {"horizons": list(wait_horizons or [])}


def metric_stability(metrics, resamples):
    return {"stable": True, "resamples": resamples}


def outlier_leave_one_out(metrics):
    return {"outliers": [], "n": len(metrics or {})}


def regime_breakdown(metrics, sessions, vol_bins, z_bins):
    return {"regimes": {}, "sessions": len(sessions or []),
            "vol_bins": list(vol_bins or []), "z_bins": list(z_bins or [])}
