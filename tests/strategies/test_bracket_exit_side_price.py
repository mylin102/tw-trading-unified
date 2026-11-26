"""2026-11-25 Hermes Agent: bracket P&L must use the leg's EXIT side, not close.

Trade mts-auto-171646-409 (far leg SHORT @37525):
    far bid 37537 / far ask 37558   (21-point book)
    close-implied PnL : 37525 - 37537 = -12    (no stop)
    real exit cost    : 37525 - 37558 = -33    (stop breached)
A configured 30-point stop therefore behaved like 30 + book width and filled
at -70. The bracket read `far_close`, which sits on the bid side.
"""
import pytest

from strategies.plugins.futures.active.tmf_spread import TMFSpread


def _s(near_side="LONG", far_side="SHORT"):
    s = TMFSpread.__new__(TMFSpread)
    s._near_side = near_side
    s._far_side = far_side
    s._near_entry = 37424.0   # trade mts-auto-171646-409
    s._far_entry = 37525.0
    return s


BAR = {"bbo_near_bid": 37410, "bbo_near_ask": 37416,
       "bbo_far_bid": 37537, "bbo_far_ask": 37558}


def test_short_leg_uses_ask():
    assert _s()._bracket_exit_price("far", BAR, 37537.0) == 37558.0


def test_long_leg_uses_bid():
    assert _s()._bracket_exit_price("near", BAR, 37413.0) == 37410.0


def test_falls_back_to_close_when_bbo_missing():
    assert _s()._bracket_exit_price("far", {}, 37537.0) == 37537.0
    assert _s()._bracket_exit_price("far", {"bbo_far_ask": None}, 37537.0) == 37537.0
    assert _s()._bracket_exit_price("far", {"bbo_far_ask": 0}, 37537.0) == 37537.0
    assert _s()._bracket_exit_price("far", {"bbo_far_ask": float("nan")}, 37537.0) == 37537.0


def test_unknown_side_falls_back_to_bid_side_price():
    """No side known -> use bid key (conservative, matches old close behaviour)."""
    assert _s(far_side=None)._bracket_exit_price("far", BAR, 37537.0) == 37537.0


def test_incident_reproduces_stop_breach_on_ask_only():
    """The 30-point stop must fire on the ask, and must NOT fire on close."""
    s = _s()
    close_pnl = s._pnl_far(37537.0)          # what the old code saw
    exit_pnl = s._pnl_far(s._bracket_exit_price("far", BAR, 37537.0))
    assert close_pnl == pytest.approx(-12.0)
    assert exit_pnl == pytest.approx(-33.0)
    assert close_pnl > -30.0                 # old code: no stop
    assert exit_pnl <= -30.0                 # new code: stop fires
