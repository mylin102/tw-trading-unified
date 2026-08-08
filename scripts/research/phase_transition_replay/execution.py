"""Execution-quality prices (replay + A4 share this contract).

EXECUTABLE_BBO requires BOTH legs with VALID bid AND ask (present, finite,
> 0) within the documented staleness bounds. Per-leg close action picks the
executable side: LONG closes at bid, SHORT closes at ask. Incomplete
evidence downgrades the tier (BOUNDED_PROXY / MARK_PROXY / NOT_AVAILABLE)
with a reason — no historical BBO, never claim executable.
"""

import math


def _valid_price(v):
    """Present, finite, positive."""
    if v is None:
        return False
    if isinstance(v, bool):
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0.0


def _close_side(side_q):
    """Executable close-side price: LONG closes at bid, SHORT at ask."""
    action = getattr(side_q, "close_action", None)
    if action == "LONG":
        return "bid"
    return "ask"  # SHORT (default) closes at ask


def executable_prices(quotes, decision_ts, staleness_bounds):
    """Classify + return executable prices for a decision point."""
    max_age = (staleness_bounds or {}).get("max_age_s", 30)
    prices = {}
    reasons = []
    for side, q in (quotes or {}).items():
        if not q:
            reasons.append(f"{side}: no quote")
            continue
        age = getattr(q, "age_s", None)
        if age is not None and age > max_age:
            reasons.append(f"{side}: stale {age}s")
            continue
        bid = getattr(q, "bid", None)
        ask = getattr(q, "ask", None)
        if not _valid_price(bid):
            reasons.append(f"{side}: bid missing/zero/NaN")
            continue
        if not _valid_price(ask):
            reasons.append(f"{side}: ask missing/zero/NaN")
            continue
        prices[side] = {"bid": float(bid), "ask": float(ask)}
    n_fresh = len(prices)
    if n_fresh >= 2:
        tier = "EXECUTABLE_BBO"
    elif n_fresh == 1:
        tier = "BOUNDED_PROXY"
    elif any((quotes or {}).values()):
        tier = "MARK_PROXY"
    else:
        tier = "NOT_AVAILABLE"
    executable = {
        side: p[_close_side(quotes[side])] for side, p in prices.items()}
    return {"tier": tier, "prices": prices,
            "executable_prices": executable,
            "decision_ts": decision_ts, "reasons": reasons}
