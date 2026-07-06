"""
Tests for theta_gang.py skew regime adaptation.

Verifies:
- select_strikes accepts skew_regime and adjusts offsets
- LEFT_SKEW -> wider put wing, tighter call wing
- RIGHT_SKEW -> wider call wing, tighter put wing
- NEUTRAL/None -> unchanged defaults
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategies.options.theta_gang import select_strikes


def _strike_list(legs):
    """Extract (side, strike, action) tuples from SpreadLeg list."""
    return [(leg.side, leg.strike, leg.action) for leg in legs]


def test_select_strikes_default():
    """Default (no skew_regime) -> unchanged behavior."""
    legs = select_strikes(34000, 100, "bull_put_spread", wing_width=200, otm_offset=200)
    result = _strike_list(legs)
    assert result[0] == ("P", 33800, "SELL")
    assert result[1] == ("P", 33600, "BUY")


def test_select_strikes_left_skew():
    """LEFT_SKEW -> put wing wider, call wing tighter."""
    skew = {"shape": "LEFT_SKEW"}
    legs = select_strikes(34000, 100, "iron_condor", wing_width=200, otm_offset=200, skew_regime=skew)
    result = _strike_list(legs)
    assert ("P", 33700, "SELL") in result
    assert ("P", 33400, "BUY") in result
    assert ("C", 34100, "SELL") in result
    assert ("C", 34200, "BUY") in result


def test_select_strikes_right_skew():
    """RIGHT_SKEW -> call wing wider, put wing tighter."""
    skew = {"shape": "RIGHT_SKEW"}
    legs = select_strikes(34000, 100, "iron_condor", wing_width=200, otm_offset=200, skew_regime=skew)
    result = _strike_list(legs)
    assert ("P", 33900, "SELL") in result
    assert ("P", 33800, "BUY") in result
    assert ("C", 34300, "SELL") in result
    assert ("C", 34600, "BUY") in result


def test_select_strikes_neutral_unchanged():
    """NEUTRAL shape -> no adjustment."""
    skew = {"shape": "NEUTRAL"}
    legs = select_strikes(34000, 100, "bull_put_spread", wing_width=200, otm_offset=200, skew_regime=skew)
    result = _strike_list(legs)
    assert result[0] == ("P", 33800, "SELL")
