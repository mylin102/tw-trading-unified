"""Required reports — skeletal (A4 v2: reuse phase-transition contracts).

Reuses phase_transition_replay.classify: four absolute Y, six pairwise
deltas, interval dominance, evidence-first classifier — never a new
ad hoc metric stack.
"""

from scripts.research.phase_transition_replay import classify as _replay_classify  # noqa: F401


def paired_delta_vs_immediate(arm, immediate):
    raise NotImplementedError("reports.paired_delta_vs_immediate")


def recovery_rate(actions, outcomes):
    raise NotImplementedError("reports.recovery_rate: meaningful recovery rate + action distribution after recovery")


def bad_execution_reduction(before, after):
    raise NotImplementedError("reports.bad_execution_reduction: bad-execution/adverse-extreme reduction")


def tail_mae_cost_of_waiting(arm, wait_horizons):
    raise NotImplementedError("reports.tail_mae_cost_of_waiting")


def metric_stability(metrics, resamples):
    raise NotImplementedError("reports.metric_stability")


def outlier_leave_one_out(metrics):
    raise NotImplementedError("reports.outlier_leave_one_out")


def regime_breakdown(metrics, sessions, vol_bins, z_bins):
    raise NotImplementedError("reports.regime_breakdown: session/volatility/z regime")
