"""Quote valuation (design §3) — strictly no look-ahead.

Rule: latest tick with ts <= target_dt (never future). Tiers:
  EXECUTABLE_BBO     persisted bid AND ask > 0 for BOTH legs
  BOUNDED_TICK_PROXY last-price fallback / one-side valid, age <= bound
  UNUSABLE           no valid quote within bound / missing pre-release tick
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

TIER_EXECUTABLE_BBO = "EXECUTABLE_BBO"
TIER_BOUNDED_TICK_PROXY = "BOUNDED_TICK_PROXY"
TIER_UNUSABLE = "UNUSABLE"

DEFAULT_AGE_BOUND_S = 5.0


def select_quote(ticks: List[dict], target_dt, age_bound_s: float = DEFAULT_AGE_BOUND_S):
    """Latest tick with ts <= target_dt (never future).

    Returns (tick, age_s) or (None, None) when no past tick exists or age
    exceeds the bound.
    """
    past = [t for t in ticks if t["ts"] <= target_dt]
    if not past:
        return None, None
    t = max(past, key=lambda x: x["ts"])
    age_s = (target_dt - t["ts"]).total_seconds()
    if age_s > age_bound_s:
        return None, None
    return t, age_s


def _leg_price(tick: dict, side: str, prefer_bid_ask: bool = True) -> Tuple[Optional[float], bool]:
    """(price, used_bid_ask). side is the CLOSE side ('BUY'/'SELL')."""
    s = (side or "").strip().upper()
    if prefer_bid_ask:
        ask = tick.get("ask") or 0
        bid = tick.get("bid") or 0
        if s == "BUY" and ask > 0:
            return float(ask), True
        if s == "SELL" and bid > 0:
            return float(bid), True
    last = tick.get("price") or 0
    if last > 0:
        return float(last), False
    return None, False


def tier_for_legs(leg_quotes: Dict[str, Tuple[dict, Optional[float]]],
                  age_bound_s: float = DEFAULT_AGE_BOUND_S) -> Tuple[str, dict]:
    """Classify the combined-leg quote tier (design §3).

    leg_quotes: {leg: (tick_or_None, age_s_or_None)}.
    Returns (tier, detail) where detail carries per-leg price/used_bid_ask.
    """
    detail = {}
    for leg, (tick, age) in leg_quotes.items():
        if tick is None or age is None or age > age_bound_s:
            detail[leg] = {"usable": False, "reason": "missing_or_stale"}
        else:
            detail[leg] = {"usable": True, "age_s": round(age, 3)}
    if not all(d["usable"] for d in detail.values()):
        return TIER_UNUSABLE, detail
    all_bid_ask = True
    for leg, (tick, _age) in leg_quotes.items():
        if not (tick.get("bid") or 0) > 0 or not (tick.get("ask") or 0) > 0:
            all_bid_ask = False
    if all_bid_ask:
        return TIER_EXECUTABLE_BBO, detail
    return TIER_BOUNDED_TICK_PROXY, detail


def valuation_price(tick: dict, side: str) -> Tuple[Optional[float], str]:
    """Per-leg valuation price for release_time_combined_valuation_gross.

    Returns (price, source) where source in {"bid", "ask", "last_price", "none"}.
    """
    px, used_bb = _leg_price(tick, side)
    if px is None:
        return None, "none"
    src = "last_price"
    ask = tick.get("ask") or 0
    bid = tick.get("bid") or 0
    if used_bb:
        s = (side or "").strip().upper()
        src = "ask" if (s == "BUY" and ask > 0) else "bid"
    return px, src
