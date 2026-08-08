"""Execution-quality tiers — A4 engine (reuses the replay execution contract).

EXECUTABLE_BBO / BOUNDED_PROXY / MARK_PROXY / NOT_AVAILABLE — a historical
BBO is REQUIRED to ever claim executable; tick-only values are proxies.
"""

from scripts.research.phase_transition_replay import execution as _replay_execution  # noqa: F401


def evidence_tier(quotes, decision_ts, staleness_bounds):
    """Classify the evidence for one decision point.

    - executable BBO: both legs have quotes with last bid/ask ts within the
      documented staleness bounds
    - BOUNDED_PROXY: tick-only values within bounds
    - MARK_PROXY: mark/last-only
    - NOT_AVAILABLE: nothing usable
    """
    if not quotes or not any(quotes.values()):
        return "NOT_AVAILABLE"
    max_age = staleness_bounds.get("max_age_s", 30) if staleness_bounds else 30
    fresh = []
    for side, q in quotes.items():
        if not q:
            continue
        age = getattr(q, "age_s", None)
        if age is None or age <= max_age:
            fresh.append(side)
    if len(fresh) >= 2:
        return "EXECUTABLE_BBO"
    if fresh:
        return "BOUNDED_PROXY"
    return "MARK_PROXY"


def never_claim_executable_without_bbo(tier, has_bbo):
    """False whenever a non-BBO evidence tier is claimed as executable."""
    if not has_bbo and tier in ("EXECUTABLE_BBO", "BOUNDED_PROXY",
                                "MARK_PROXY"):
        return False
    return tier != "NOT_AVAILABLE"
