"""Candidate pipeline — skeletal; REUSES committed exit_attribution components."""

from scripts.research.exit_attribution.reconcile import reconcile_fill  # noqa: F401
from scripts.research.exit_attribution.quoting import select_quote  # noqa: F401
reconcile = reconcile_fill


def candidate_timestamps(release_event):
    raise NotImplementedError("pipeline.candidate_timestamps: decision/ORDER_SUBMITTED ts + fill ts + skew")


def censored_with_reason(candidate, reason):
    raise NotImplementedError("pipeline.censored_with_reason: censored list, never silently dropped")


def time_stop_triggered(horizon_net, net_at_release):
    raise NotImplementedError("pipeline.time_stop_triggered: trigger only if horizon net < net_at_release")
