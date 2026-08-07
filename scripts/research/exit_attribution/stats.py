"""Statistics (design §6) — exact sign test from ACTUAL signs + sensitivity.

The 8/6 bug (p = 1/(2^N) hardcode) is regression-guarded by
test_exit_attribution_stats.py::test_sign_test_3pos4neg_exact.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from math import comb
from typing import List, Optional, Tuple


def exact_sign_test_p(deltas: List[float]) -> Optional[float]:
    """Two-sided exact binomial sign test from ACTUAL non-zero signs.

    Zero deltas are excluded (reported separately by the caller).
    p = 2 * sum_{i=0..min(pos,neg)} C(n,i) / 2^n, capped at 1.0.
    Returns None when there are no non-zero deltas (insufficient data).
    """
    pos = sum(1 for d in deltas if d > 0)
    neg = sum(1 for d in deltas if d < 0)
    n = pos + neg
    if n == 0:
        return None
    k = min(pos, neg)
    p = 2.0 * sum(comb(n, i) for i in range(k + 1)) / (2 ** n)
    return min(p, 1.0)


def split_nonzero(deltas: List[float]) -> dict:
    """Count positives/negatives/zeros; used for coverage reporting."""
    pos = sum(1 for d in deltas if d > 0)
    neg = sum(1 for d in deltas if d < 0)
    zero = sum(1 for d in deltas if d == 0)
    return {"positive": pos, "negative": neg, "zero": zero}


def apply_adverse_tick(price: float, side: str, tick_size: float) -> float:
    """Directionally adverse ±1 tick (design §6): LONG exit down, SHORT up."""
    s = (side or "").strip().upper()
    if s in ("SELL", "LONG"):
        return price - tick_size  # LONG close (SELL) gets worse when price drops
    return price + tick_size  # SHORT close (BUY) gets worse when price rises


def fixed_event_latency_quote(
    ticks: List[dict],
    release_ts,
    delay_s: float,
    window_s: float = 10.0,
    side: str = "SELL",
) -> Tuple[Optional[float], str, Optional[str]]:
    """Pure fixed-event latency (design §6): FIRST valid observation at/after
    release_ts + delay_s, within a bounded window.

    Side-aware (codex blocker #5): BUY values the ask, SELL values the bid;
    last price is used ONLY as a proxy and reported as source 'last_price'.
    Returns (price, ts_or_NOT_AVAILABLE, source).
    """
    s = (side or "").strip().upper()
    start = release_ts + timedelta(seconds=delay_s)
    end = start + timedelta(seconds=window_s)
    for t in ticks:
        if not (start <= t["ts"] <= end):
            continue
        if s == "BUY" and (t.get("ask") or 0) > 0:
            return float(t["ask"]), str(t["ts"]), "ask"
        if s == "SELL" and (t.get("bid") or 0) > 0:
            return float(t["bid"]), str(t["ts"]), "bid"
        if t.get("price"):
            return float(t["price"]), str(t["ts"]), "last_price"
    return None, "NOT_AVAILABLE", None


def adversarial_latency_envelope(
    initial_px: float, later_px: Optional[float], side: str
) -> float:
    """Separately-named worst-of-two envelope (design §6, ADVERSARIAL_...).

    Never merged into the pure-latency result. LONG close (SELL): worse =
    lower of the two; SHORT close (BUY): worse = higher of the two.
    """
    if later_px is None:
        return initial_px
    s = (side or "").strip().upper()
    if s in ("SELL", "LONG"):
        return min(initial_px, later_px)
    return max(initial_px, later_px)
