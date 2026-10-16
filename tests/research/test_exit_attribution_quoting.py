"""T9-T12: quote valuation (design §3)."""
from datetime import datetime, timedelta
import pytest

from scripts.research.exit_attribution.quoting import (
    TIER_BOUNDED_TICK_PROXY,
    TIER_EXECUTABLE_BBO,
    TIER_UNUSABLE,
    select_quote,
    tier_for_legs,
    valuation_price,
)

T0 = datetime(2026, 8, 6, 12, 0, 0)


def _tick(ts, bid=0, ask=0, last=0):
    return {"ts": ts, "bid": bid, "ask": ask, "price": last}


def test_t9_lookahead_guard_future_only_unusable():
    # all ticks AFTER release_ts -> never used
    ticks = [_tick(T0 + timedelta(seconds=5), 100, 101, 100.5)]
    tick, age = select_quote(ticks, T0)
    assert tick is None
    tier, _ = tier_for_legs({"NEAR": (tick, age), "FAR": (tick, age)})
    assert tier == TIER_UNUSABLE


def test_t10_executable_bbo_both_legs():
    near = _tick(T0, 100, 101, 100.5)
    far = _tick(T0, 200, 201, 200.5)
    tier, detail = tier_for_legs(
        {"NEAR": (near, 0.1), "FAR": (far, 0.2)}, 5.0
    )
    assert tier == TIER_EXECUTABLE_BBO
    px, src = valuation_price(near, "BUY")
    assert px == pytest.approx(101.0) and src == "ask"


def test_t11_proxy_last_price_fallback():
    near = _tick(T0, 0, 0, 100.5)  # no bid/ask -> last price
    far = _tick(T0, 200, 201, 200.5)
    tier, _ = tier_for_legs({"NEAR": (near, 0.1), "FAR": (far, 0.2)}, 5.0)
    assert tier == TIER_BOUNDED_TICK_PROXY
    px, src = valuation_price(near, "SELL")
    assert px == pytest.approx(100.5) and src == "last_price"


def test_t12_stale_or_missing_unusable():
    near = _tick(T0 - timedelta(seconds=30), 100, 101, 100.5)  # age 30s > bound
    far = _tick(T0, 200, 201, 200.5)
    tier, _ = tier_for_legs({"NEAR": (near, 30.0), "FAR": (far, 0.2)}, 5.0)
    assert tier == TIER_UNUSABLE
    # missing leg entirely
    tier2, _ = tier_for_legs({"NEAR": (None, None), "FAR": (far, 0.2)}, 5.0)
    assert tier2 == TIER_UNUSABLE
