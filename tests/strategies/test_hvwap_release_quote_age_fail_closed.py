"""2026-11-10 Hermes Agent: _hvwap_release_quote_age_ok() must fail closed
when the quote-age field is absent, matching the baseline freshness gate's
contract (_restart_baseline_quote, same file: "an unprovable age fails
closed").

Found via _record_release_counterfactual (added 2026-10-21) disagreeing
with this gate on the SAME bars in production (Mini PAPER logs): the
counterfactual recorder correctly marked bbo_status=MISSING when
near_tick_age_ms/far_tick_age_ms were absent from the bar dict (the
heartbeat/5-min bar path lacks these keys entirely), while this gate's old
`float(_age or 0.0)` silently treated the missing key as "age=0ms" and let
the release proceed with zero evidence the quote was fresh.
"""
import math

import pytest

import strategies.plugins.futures.active.tmf_spread as tmf


def _strategy(max_quote_age_ms=1000.0):
    s = object.__new__(tmf.TMFSpread)
    s._max_quote_age_ms = max_quote_age_ms
    return s


@pytest.mark.parametrize("rel_leg,key", [("NEAR", "near_tick_age_ms"),
                                          ("FAR", "far_tick_age_ms")])
def test_missing_age_key_fails_closed(rel_leg, key):
    """The exact production shape: bar dict (e.g. from the 5-min heartbeat
    path) lacks the age key entirely -- must NOT be treated as fresh."""
    s = _strategy()
    ok, age = s._hvwap_release_quote_age_ok({}, rel_leg)
    assert ok is False
    assert math.isnan(age)


@pytest.mark.parametrize("rel_leg,key", [("NEAR", "near_tick_age_ms"),
                                          ("FAR", "far_tick_age_ms")])
def test_age_zero_is_still_allowed(rel_leg, key):
    """A REAL age=0 (genuinely just received) must still pass -- the fix
    must not punish legitimate fresh quotes, only missing evidence."""
    s = _strategy()
    ok, age = s._hvwap_release_quote_age_ok({key: 0.0}, rel_leg)
    assert ok is True
    assert age == 0.0


@pytest.mark.parametrize("rel_leg,key", [("NEAR", "near_tick_age_ms"),
                                          ("FAR", "far_tick_age_ms")])
def test_age_within_limit_allowed(rel_leg, key):
    s = _strategy(max_quote_age_ms=1000.0)
    ok, age = s._hvwap_release_quote_age_ok({key: 500.0}, rel_leg)
    assert ok is True
    assert age == 500.0


@pytest.mark.parametrize("rel_leg,key", [("NEAR", "near_tick_age_ms"),
                                          ("FAR", "far_tick_age_ms")])
def test_age_beyond_limit_blocked(rel_leg, key):
    s = _strategy(max_quote_age_ms=1000.0)
    ok, age = s._hvwap_release_quote_age_ok({key: 1500.0}, rel_leg)
    assert ok is False
    assert age == 1500.0


@pytest.mark.parametrize("bad_value", [float("nan"), -1.0, "garbage", True])
def test_invalid_age_values_fail_closed(bad_value):
    s = _strategy()
    ok, age = s._hvwap_release_quote_age_ok({"near_tick_age_ms": bad_value}, "NEAR")
    assert ok is False


def test_only_the_released_legs_key_is_consulted():
    """Decoupled-leg semantics must be preserved by the fix: a fresh FAR
    age must not matter when releasing NEAR, and vice versa."""
    s = _strategy()
    ok, age = s._hvwap_release_quote_age_ok(
        {"near_tick_age_ms": 100.0, "far_tick_age_ms": None}, "NEAR")
    assert ok is True
    ok, age = s._hvwap_release_quote_age_ok(
        {"near_tick_age_ms": None, "far_tick_age_ms": 100.0}, "FAR")
    assert ok is True
