"""The single entry-BBO reject mapper: every reason it can return."""
import pytest

from strategies.futures.monitor import _bbo_reject_reason

OK = {"bbo_near_bid": 1.0, "bbo_near_ask": 1.1,
      "bbo_far_bid": 2.0, "bbo_far_ask": 2.1,
      "bbo_near_age_ms": 10, "bbo_far_age_ms": 20}


def _bar(**over):
    d = dict(OK); d.update(over); return d


@pytest.mark.parametrize("key", ["bbo_near_bid", "bbo_near_ask", "bbo_far_bid", "bbo_far_ask"])
def test_missing_side_is_bbo_missing(key):
    assert _bbo_reject_reason(_bar(**{key: None}), 30000) == "BBO_MISSING"


@pytest.mark.parametrize("key", ["bbo_near_age_ms", "bbo_far_age_ms"])
def test_missing_age_is_bbo_missing(key):
    assert _bbo_reject_reason(_bar(**{key: None}), 30000) == "BBO_MISSING"


def test_nonfinite_is_reported():
    assert _bbo_reject_reason(_bar(bbo_far_bid=float("nan")), 30000) == "BBO_NONFINITE"


def test_non_numeric_is_reported():
    assert _bbo_reject_reason(_bar(bbo_far_bid="x"), 30000) == "BBO_NONFINITE"


def test_non_positive_is_invalid():
    assert _bbo_reject_reason(_bar(bbo_near_bid=0.0), 30000) == "BBO_INVALID"


def test_aged_quote_is_stale():
    assert _bbo_reject_reason(_bar(bbo_near_age_ms=30001), 30000) == "STALE_QUOTE"


def test_healthy_bar_passes():
    assert _bbo_reject_reason(dict(OK), 30000) is None
