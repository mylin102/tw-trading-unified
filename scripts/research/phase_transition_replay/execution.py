"""Execution-quality prices (replay + A4 share this contract).

EXECUTABLE_BBO requires EXACTLY the expected leg set (near/far), each with
VALID bid AND ask (present, finite, > 0), FRESH (age present, finite,
>= 0, within bounds) and an EXPLICIT close_action (LONG/SHORT). Everything
else downgrades the tier (BOUNDED_PROXY / MARK_PROXY / NOT_AVAILABLE) with
a reason — no historical BBO, never claim executable.
"""

import math

EXPECTED_LEGS = ("near", "far")


def _valid_price(v):
    """Present, finite, positive."""
    if v is None or isinstance(v, bool):
        return False
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f) and f > 0.0


def _age_ok(q, max_age):
    """Fail-closed freshness: age missing/NaN/negative/timeout is NOT ok."""
    age = q.get("age_s") if isinstance(q, dict) else getattr(q, "age_s", None)
    if age is None:
        return False, "age missing"
    try:
        a = float(age)
    except (TypeError, ValueError):
        return False, f"age unparseable {age!r}"
    if not math.isfinite(a):
        return False, f"age NaN/inf"
    if a < 0.0:
        return False, f"age negative {a}"
    if a > max_age:
        return False, f"stale {a}s > {max_age}s"
    return True, None


def _close_action(q):
    """Explicit LONG/SHORT only — anything else is fail-closed."""
    action = (q.get("close_action") if isinstance(q, dict)
              else getattr(q, "close_action", None))
    if action not in ("LONG", "SHORT"):
        return None, f"close_action {action!r} not LONG/SHORT"
    return action, None


def executable_prices(quotes, decision_ts, staleness_bounds):
    """Classify + return executable prices for a decision point."""
    max_age = (staleness_bounds or {}).get("max_age_s", 30)
    quotes = dict(quotes or {})
    keys = set(quotes)
    if keys != set(EXPECTED_LEGS):
        return {"tier": "NOT_AVAILABLE", "prices": {},
                "executable_prices": {}, "decision_ts": decision_ts,
                "reasons": [
                    "leg set mismatch: "
                    f"missing={sorted(set(EXPECTED_LEGS) - keys)} "
                    f"extra={sorted(keys - set(EXPECTED_LEGS))}"]}
    prices = {}
    reasons = []
    for side in EXPECTED_LEGS:
        q = quotes[side]
        if not q:
            reasons.append(f"{side}: no quote")
            continue
        ok, why = _age_ok(q, max_age)
        if not ok:
            reasons.append(f"{side}: {why}")
            continue
        bid = q.get("bid") if isinstance(q, dict) else getattr(q, "bid", None)
        ask = q.get("ask") if isinstance(q, dict) else getattr(q, "ask", None)
        if not _valid_price(bid):
            reasons.append(f"{side}: bid missing/zero/NaN")
            continue
        if not _valid_price(ask):
            reasons.append(f"{side}: ask missing/zero/NaN")
            continue
        action, why = _close_action(q)
        if action is None:
            reasons.append(f"{side}: {why}")
            continue
        prices[side] = {"bid": float(bid), "ask": float(ask),
                        "close_action": action}
    n = len(prices)
    if n == 2:
        tier = "EXECUTABLE_BBO"
    elif n == 1:
        tier = "BOUNDED_PROXY"
    else:
        tier = "NOT_AVAILABLE"
    executable = {
        side: (p["bid"] if p["close_action"] == "LONG" else p["ask"])
        for side, p in prices.items()}
    return {"tier": tier, "prices": prices,
            "executable_prices": executable,
            "decision_ts": decision_ts, "reasons": reasons}
