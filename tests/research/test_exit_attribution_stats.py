"""T15-T18: statistics (design §6)."""
from datetime import datetime, timedelta
import pytest

from scripts.research.exit_attribution.stats import (
    adversarial_latency_envelope,
    apply_adverse_tick,
    exact_sign_test_p,
    fixed_event_latency_quote,
    split_nonzero,
)


def test_t15_sign_test_3pos4neg_exact_p_equals_1():
    # 3 pos / 4 neg, n=7: two-sided p = 2*(C7,0+C7,1+C7,2+C7,3)/2^7 = 2*64/128 = 1.0
    deltas = [34.8, 34.7, 354.5, -1465.6, -2055.4, -175.2, -75.8]
    p = exact_sign_test_p(deltas)
    assert p == pytest.approx(1.0)  # exact assertion, not merely != 0.0078125


def test_t16_zero_deltas_excluded_and_all_zero_no_claim():
    deltas = [1.0, -1.0, 0.0, 0.0]
    assert split_nonzero(deltas) == {"positive": 1, "negative": 1, "zero": 2}
    assert exact_sign_test_p(deltas) == pytest.approx(1.0)  # zeros excluded
    assert exact_sign_test_p([0.0, 0.0, 0.0]) is None  # insufficient -> no claim


def test_t17_fixed_event_latency_first_valid_observation():
    T0 = datetime(2026, 8, 6, 12, 0, 0)
    ticks = [
        {"ts": T0 + timedelta(seconds=1), "ask": 0, "bid": 0, "price": 100.0},
        {"ts": T0 + timedelta(seconds=5), "ask": 101.0, "bid": 100.5, "price": 100.8},
        {"ts": T0 + timedelta(seconds=30), "ask": 102.0, "bid": 101.5, "price": 101.8},
    ]
    # SELL side -> bid
    px, ts, src = fixed_event_latency_quote(ticks, T0, delay_s=2.0, window_s=10.0, side="SELL")
    assert px == pytest.approx(100.5)  # bid, FIRST valid at/after T0+2s
    assert ts == str(ticks[1]["ts"]) and src == "bid"
    # BUY side -> ask
    px2, _ts2, src2 = fixed_event_latency_quote(ticks, T0, delay_s=2.0, window_s=10.0, side="BUY")
    assert px2 == pytest.approx(101.0) and src2 == "ask"
    # no valid observation in window (next tick at 5s > end T0+3s) -> NOT_AVAILABLE
    px3, ts3, src3 = fixed_event_latency_quote(ticks, T0, delay_s=2.0, window_s=1.0)
    assert px3 is None and ts3 == "NOT_AVAILABLE" and src3 is None


def test_t17b_adversarial_envelope_separate():
    # LONG close (SELL): worse = lower; SHORT close (BUY): worse = higher
    assert adversarial_latency_envelope(100.0, 99.0, "SELL") == 99.0
    assert adversarial_latency_envelope(100.0, 101.0, "SELL") == 100.0
    assert adversarial_latency_envelope(100.0, 101.0, "BUY") == 101.0
    assert adversarial_latency_envelope(100.0, 99.0, "BUY") == 100.0
    # missing later quote -> initial only
    assert adversarial_latency_envelope(100.0, None, "SELL") == 100.0


def test_t18_adverse_tick_direction():
    # LONG exit (SELL) gets worse when price drops: quote - tick
    assert apply_adverse_tick(100.0, "SELL", 0.5) == pytest.approx(99.5)
    # SHORT exit (BUY) gets worse when price rises: quote + tick
    assert apply_adverse_tick(100.0, "BUY", 0.5) == pytest.approx(100.5)
