"""Execution-quality prices (replay + A4 share this contract).

EXECUTABLE_BBO requires EXACTLY the expected leg set (near/far), each with
VALID bid AND ask (present, finite, > 0), FRESH (age present, finite,
>= 0, within bounds), an EXPLICIT close_action (LONG/SHORT) AND — when
pair synchronization is configured (max_pair_skew_ms in staleness_bounds)
— synchronized quotes (both legs carry a valid quote_exchange_ts, neither
later than the decision time, abs(skew) <= bound). Everything else
downgrades the tier (BOUNDED_PROXY / MARK_PROXY / NOT_AVAILABLE) with a
reason — no historical BBO, never claim executable.

Quote inputs may be objects OR JSON dicts — normalized via
_normalize_quote; ALL field access is getattr-safe (malformed objects fail
closed with a reason, never AttributeError).
"""

import math
from types import SimpleNamespace

EXPECTED_LEGS = ("near", "far")

# strict epoch-ms domain (v6.1): covers 2014-05-13 .. 2049-04-16 in ms;
# seconds (~1e9), microseconds (~1e15) and nanoseconds (~1e18) fall outside
EPOCH_MS_MIN = 1_400_000_000_000
EPOCH_MS_MAX = 2_500_000_000_000


def validate_epoch_ms(v):
    """Strict epoch-ms validator.
    Rejects bool (an int subclass), non-ints, and any value outside the
    plausible epoch-ms range — seconds/microseconds/nanoseconds scales
    cannot pass. Returns bool.
    """
    if isinstance(v, bool) or not isinstance(v, int):
        return False
    return EPOCH_MS_MIN <= v <= EPOCH_MS_MAX


def _normalize_quote(q):
    """dict -> SimpleNamespace (JSON input support); others pass through."""
    if isinstance(q, dict):
        return SimpleNamespace(**q)
    return q


def _get(q, name):
    """getattr-safe field access — never raises AttributeError."""
    try:
        return getattr(q, name, None)
    except Exception:  # pragma: no cover - any exotic object
        return None


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
    age = _get(_normalize_quote(q), "age_s")
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
    action = _get(_normalize_quote(q), "close_action")
    if action not in ("LONG", "SHORT"):
        return None, f"close_action {action!r} not LONG/SHORT"
    return action, None


def _pair_sync(quotes_norm, decision_ts_ms, max_pair_skew_ms):
    """Synchronized-BBO check. Returns (ok, reason, skew).

    Both legs must carry a valid quote_exchange_ts, neither later than the
    decision time, and abs(skew) <= bound.
    """
    ts = {}
    for side in EXPECTED_LEGS:
        t = _get(quotes_norm[side], "quote_exchange_ts")
        if not validate_epoch_ms(t):
            return False, (f"{side} quote_exchange_ts invalid epoch-ms: "
                           f"{t!r}"), None
        ts[side] = t
    if decision_ts_ms is not None:
        for side, t in ts.items():
            if t > decision_ts_ms:
                return False, (f"{side} quote_exchange_ts {t} later than "
                               f"decision {decision_ts_ms}"), None
    skew = abs(ts["near"] - ts["far"])
    if skew > max_pair_skew_ms:
        return False, f"pair skew {skew}ms > {max_pair_skew_ms}ms", skew
    return True, None, skew


def executable_prices(quotes, decision_ts, staleness_bounds):
    """Classify + return executable prices for a decision point.

    staleness_bounds may carry max_age_s and max_pair_skew_ms; when
    max_pair_skew_ms is configured, synchronized quotes are REQUIRED for
    EXECUTABLE_BBO (decision_ts is the epoch-ms decision time domain).
    """
    bounds = staleness_bounds or {}
    max_age = bounds.get("max_age_s", 30)
    max_pair_skew_ms = bounds.get("max_pair_skew_ms")
    decision_ts_ms = bounds.get("decision_ts_ms")
    quotes = dict(quotes or {})
    keys = set(quotes)
    if keys != set(EXPECTED_LEGS):
        return {"tier": "NOT_AVAILABLE", "prices": {},
                "executable_prices": {}, "decision_ts": decision_ts,
                "reasons": [
                    "leg set mismatch: "
                    f"missing={sorted(set(EXPECTED_LEGS) - keys)} "
                    f"extra={sorted(keys - set(EXPECTED_LEGS))}"]}
    norm = {side: _normalize_quote(quotes[side]) for side in EXPECTED_LEGS}
    # synchronized-BBO requirement (when configured)
    if max_pair_skew_ms is not None:
        ok, why, skew = _pair_sync(norm, decision_ts_ms, max_pair_skew_ms)
        if not ok:
            return {"tier": "NOT_AVAILABLE", "prices": {},
                    "executable_prices": {}, "decision_ts": decision_ts,
                    "pair_skew_ms": skew, "max_pair_skew_ms": max_pair_skew_ms,
                    "reasons": [why]}
    prices = {}
    reasons = []
    for side in EXPECTED_LEGS:
        q = norm[side]
        if not q:
            reasons.append(f"{side}: no quote")
            continue
        ok, why = _age_ok(q, max_age)
        if not ok:
            reasons.append(f"{side}: {why}")
            continue
        if not _valid_price(_get(q, "bid")):
            reasons.append(f"{side}: bid missing/zero/NaN")
            continue
        if not _valid_price(_get(q, "ask")):
            reasons.append(f"{side}: ask missing/zero/NaN")
            continue
        action, why = _close_action(q)
        if action is None:
            reasons.append(f"{side}: {why}")
            continue
        prices[side] = {"bid": float(_get(q, "bid")),
                        "ask": float(_get(q, "ask")),
                        "close_action": action,
                        "quote_exchange_ts": _get(q, "quote_exchange_ts"),
                        "age_s": _get(q, "age_s")}
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
            "decision_ts": decision_ts, "reasons": reasons,
            "max_pair_skew_ms": max_pair_skew_ms}
