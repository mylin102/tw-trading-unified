"""Replay pipeline helpers — research-only.

Reuses the committed exit_attribution.reconcile_fill (no ad-hoc parser).
"""

from scripts.research.exit_attribution.reconcile import reconcile_fill  # noqa: F401

reconcile = reconcile_fill


def candidate_timestamps(release_event):
    """Decision time (not fill time) anchors the candidate; skew = fill -
    decision (execution latency evidence)."""
    decision_ts = release_event.get("decision_ts")
    fill_ts = release_event.get("fill_ts")
    return {"decision_ts": decision_ts, "fill_ts": fill_ts,
            "skew": fill_ts - decision_ts}


def censored_with_reason(candidate, reason):
    """Censored candidates are NEVER dropped silently — the record is
    preserved and flagged with the reason."""
    return {"candidate": candidate, "censored": True, "reason": reason}


def time_stop_triggered(horizon_net, net_at_release):
    """Trigger only when the horizon net is WORSE than the net at release."""
    return horizon_net < net_at_release
