"""Execution-quality prices (replay + A4 share this contract).

EXECUTABLE_BBO requires EXACTLY the expected leg set (near/far), each with
VALID bid AND ask (present, finite, > 0), FRESH (age present, finite,
>= 0, within bounds) and an EXPLICIT close_action (LONG/SHORT). Everything
else downgrades the tier (BOUNDED_PROXY / MARK_PROXY / NOT_AVAILABLE) with
a reason — no historical BBO, never claim executable.

Quote inputs may be objects (SimpleNamespace) OR JSON dicts — normalized
via _normalize_quote before any field access.
"""

import math
from types import SimpleNamespace

EXPECTED_LEGS = ("near", "far")


def _normalize_quote(q):
    """dict -> SimpleNamespace (JSON input support); others pass through."""
    if isinstance(q, dict):
        return SimpleNamespace(**q)
    return q


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
    q = _normalize_quote(q)
    age = q.age_s
    if age is None:
        return False, "age missing"
    try:
        a = float(age)
    except (TypeError, ValueError):
        return False, f"age unparseable {age!r}"
    if not math.isfinite(a):
        return False, "age NaN/inf"
    if a < 0.0:
        return False, f"age negative {a}"
    if a > max_age:
        return False, f"stale {a}s > {max_age}s"
    return True, None


def _close_action(q):
    """Explicit LONG/SHORT only — anything else is fail-closed."""
    q = _normalize_quote(q)
    action = q.close_action
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
        q = _normalize_quote(quotes[side])
        if not q:
            reasons.append(f"{side}: no quote")
            continue
        ok, why = _age_ok(q, max_age)
        if not ok:
            reasons.append(f"{side}: {why}")
            continue
        if not _valid_price(q.bid):
            reasons.append(f"{side}: bid missing/zero/NaN")
            continue
        if not _valid_price(q.ask):
            reasons.append(f"{side}: ask missing/zero/NaN")
            continue
        action, why = _close_action(q)
        if action is None:
            reasons.append(f"{side}: {why}")
            continue
        prices[side] = {"bid": float(q.bid), "ask": float(q.ask),
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
