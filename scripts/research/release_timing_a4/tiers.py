"""Execution-quality tiers — skeletal (reuses replay execution contract)."""

from scripts.research.phase_transition_replay import execution as _replay_execution  # noqa: F401


def evidence_tier(quotes, decision_ts, staleness_bounds):
    raise NotImplementedError("tiers.evidence_tier: EXECUTABLE_BBO / BOUNDED_PROXY / MARK_PROXY / NOT_AVAILABLE")


def never_claim_executable_without_bbo(tier, has_bbo):
    raise NotImplementedError("tiers.never_claim_executable_without_bbo")
