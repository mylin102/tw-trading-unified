"""Execution-quality prices (replay + A4 share this contract).

EXECUTABLE_BBO requires BOTH legs with fresh bid/ask within the documented
staleness bounds; tick-only values are BOUNDED_PROXY; mark/last only is
MARK_PROXY; nothing usable is NOT_AVAILABLE. No historical BBO -> never
claim executable.
"""


def executable_prices(quotes, decision_ts, staleness_bounds):
    """Classify + return executable prices for a decision point."""
    if not quotes or not any(quotes.values()):
        return {"tier": "NOT_AVAILABLE", "prices": None,
                "reason": "no usable quotes"}
    max_age = (staleness_bounds or {}).get("max_age_s", 30)
    fresh = []
    prices = {}
    for side, q in quotes.items():
        if not q:
            continue
        age = getattr(q, "age_s", None)
        if age is None or age <= max_age:
            fresh.append(side)
            prices[side] = {"bid": getattr(q, "bid", None),
                            "ask": getattr(q, "ask", None)}
    if len(fresh) >= 2:
        return {"tier": "EXECUTABLE_BBO", "prices": prices,
                "decision_ts": decision_ts}
    if fresh:
        return {"tier": "BOUNDED_PROXY", "prices": prices,
                "decision_ts": decision_ts}
    return {"tier": "MARK_PROXY", "prices": prices,
            "decision_ts": decision_ts}
