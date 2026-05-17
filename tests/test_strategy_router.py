"""
Tests for core/derivatives/strategy_router.py

Verifies all 8+ combinations from the decision matrix:
- LEFT_SKEW + DOWN -> BEARISH
- LEFT_SKEW + NEUTRAL -> HEDGING
- LEFT_SKEW + UP -> UNKNOWN (conflict)
- RIGHT_SKEW + UP -> BULLISH
- RIGHT_SKEW + NEUTRAL -> INCOME
- RIGHT_SKEW + DOWN -> UNKNOWN (conflict)
- PARALLEL + any -> VOLATILITY
- NEUTRAL + any -> RANGE
- UNKNOWN/None -> UNKNOWN
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.derivatives.strategy_router import route_strategy


def _skew(shape, confidence=0.7):
    return {"shape": shape, "confidence": confidence}


# ---------------------------------------------------------------------------
# LEFT_SKEW
# ---------------------------------------------------------------------------

def test_left_skew_down():
    r = route_strategy(_skew("LEFT_SKEW", 0.7), "DOWN")
    assert r["category"] == "BEARISH"
    assert r["confidence"] > 0
    assert len(r["suggested_strategies"]) > 0


def test_left_skew_neutral():
    r = route_strategy(_skew("LEFT_SKEW"), "NEUTRAL")
    assert r["category"] == "HEDGING"
    assert len(r["suggested_strategies"]) > 0


def test_left_skew_up():
    """Conflict: LEFT_SKEW + UP -> UNKNOWN."""
    r = route_strategy(_skew("LEFT_SKEW"), "UP")
    assert r["category"] == "UNKNOWN"
    assert r["confidence"] == 0.0


# ---------------------------------------------------------------------------
# RIGHT_SKEW
# ---------------------------------------------------------------------------

def test_right_skew_up():
    r = route_strategy(_skew("RIGHT_SKEW", 0.8), "UP")
    assert r["category"] == "BULLISH"
    assert r["confidence"] > 0
    assert len(r["suggested_strategies"]) > 0


def test_right_skew_neutral():
    r = route_strategy(_skew("RIGHT_SKEW"), "NEUTRAL")
    assert r["category"] == "INCOME"
    assert len(r["suggested_strategies"]) > 0


def test_right_skew_down():
    """Conflict: RIGHT_SKEW + DOWN -> UNKNOWN."""
    r = route_strategy(_skew("RIGHT_SKEW"), "DOWN")
    assert r["category"] == "UNKNOWN"
    assert r["confidence"] == 0.0


# ---------------------------------------------------------------------------
# PARALLEL
# ---------------------------------------------------------------------------

def test_parallel_any_trend():
    """Parallel + any trend -> VOLATILITY."""
    for trend in ("UP", "DOWN", "NEUTRAL"):
        r = route_strategy(_skew("PARALLEL"), trend)
        assert r["category"] == "VOLATILITY", f"Failed for trend={trend}"
        assert len(r["suggested_strategies"]) > 0


# ---------------------------------------------------------------------------
# NEUTRAL
# ---------------------------------------------------------------------------

def test_neutral_any_trend():
    """Neutral + any trend -> RANGE."""
    for trend in ("UP", "DOWN", "NEUTRAL"):
        r = route_strategy(_skew("NEUTRAL"), trend)
        assert r["category"] == "RANGE", f"Failed for trend={trend}"
        assert len(r["suggested_strategies"]) > 0


def test_neutral_low_confidence():
    """Neutral with very low confidence -> still RANGE."""
    r = route_strategy(_skew("NEUTRAL", 0.05), "NEUTRAL")
    assert r["category"] == "RANGE"


# ---------------------------------------------------------------------------
# UNKNOWN / None
# ---------------------------------------------------------------------------

def test_none_input():
    """None skew_regime -> UNKNOWN."""
    r = route_strategy(None, "NEUTRAL")
    assert r["category"] == "UNKNOWN"
    assert r["confidence"] == 0.0


def test_missing_shape():
    """Dict without shape key -> UNKNOWN."""
    r = route_strategy({"confidence": 0.5}, "DOWN")
    assert r["category"] == "UNKNOWN"


def test_empty_dict():
    r = route_strategy({}, "UP")
    assert r["category"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# Confidence propagation
# ---------------------------------------------------------------------------

def test_confidence_bullish_amplified():
    """BEARISH/BULLISH confidence amplified."""
    r = route_strategy(_skew("LEFT_SKEW", 0.7), "DOWN")
    # confidence = min(0.7 * 1.1, 1.0) = 0.77
    assert 0.70 < r["confidence"] <= 1.0


def test_confidence_non_directional():
    """Non-directional categories keep original confidence."""
    r = route_strategy(_skew("RIGHT_SKEW", 0.6), "NEUTRAL")
    assert abs(r["confidence"] - 0.6) < 0.01


# ---------------------------------------------------------------------------
# Output structure
# ---------------------------------------------------------------------------

def test_output_keys():
    """Output has all required keys."""
    r = route_strategy(_skew("NEUTRAL"), "NEUTRAL")
    required = {"category", "description", "confidence", "reason", "suggested_strategies", "input"}
    assert set(r.keys()) >= required, f"Missing keys: {required - set(r.keys())}"
