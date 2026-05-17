"""
Tests for core/derivatives/shape_classifier.py

Verifies classification for VolatilityContext:
- directional_skew: LEFT / RIGHT / SYMMETRIC
- tension: LOW / MEDIUM / HIGH
- Bounded: slope_ratio always in [-1, 1]
- Velocity tracking: delta_slope_ratio
- Legacy shape compat
"""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.derivatives.shape_classifier import IVShapeClassifier, VolatilityContext


# ---------------------------------------------------------------------------
# Directional Skew Tests
# ---------------------------------------------------------------------------

def test_left_skew():
    """Put wing >> call wing -> directional_skew = LEFT."""
    c = IVShapeClassifier()
    r = c.classify(atm_iv=0.15, otm_put_iv=0.23, otm_call_iv=0.16, underlying_price=34000)
    assert r.directional_skew == "LEFT"
    assert r.slope_ratio < -0.3
    assert r.confidence > 0.3


def test_right_skew():
    """Call wing >> put wing -> directional_skew = RIGHT."""
    c = IVShapeClassifier()
    r = c.classify(atm_iv=0.15, otm_put_iv=0.16, otm_call_iv=0.23, underlying_price=34000)
    assert r.directional_skew == "RIGHT"
    assert r.slope_ratio > 0.3


def test_symmetric_small_slopes():
    """Both wings similar, small slopes -> SYMMETRIC."""
    c = IVShapeClassifier()
    r = c.classify(atm_iv=0.15, otm_put_iv=0.17, otm_call_iv=0.17, underlying_price=34000)
    assert r.directional_skew == "SYMMETRIC"
    assert abs(r.slope_ratio) < 0.1


# ---------------------------------------------------------------------------
# Tension Tests
# ---------------------------------------------------------------------------

def test_tension_low():
    """atm_iv_change < TENSION_LOW -> LOW tension."""
    c = IVShapeClassifier()
    c.classify(atm_iv=0.15, otm_put_iv=0.17, otm_call_iv=0.17, underlying_price=34000)
    r = c.classify(atm_iv=0.151, otm_put_iv=0.171, otm_call_iv=0.171, underlying_price=34000)
    # atm_iv_change = 0.001 < 0.01
    assert r.tension == "LOW"


def test_tension_medium():
    """atm_iv_change between thresholds -> MEDIUM tension."""
    c = IVShapeClassifier()
    c.classify(atm_iv=0.15, otm_put_iv=0.17, otm_call_iv=0.17, underlying_price=34000)
    r = c.classify(atm_iv=0.165, otm_put_iv=0.185, otm_call_iv=0.185, underlying_price=34000)
    # atm_iv_change = 0.015, 0.01 < 0.015 < 0.03
    assert r.tension == "MEDIUM"


def test_tension_high():
    """atm_iv_change > TENSION_HIGH -> HIGH tension."""
    c = IVShapeClassifier()
    c.classify(atm_iv=0.15, otm_put_iv=0.17, otm_call_iv=0.17, underlying_price=34000)
    r = c.classify(atm_iv=0.19, otm_put_iv=0.21, otm_call_iv=0.21, underlying_price=34000)
    # atm_iv_change = 0.04 > 0.03
    assert r.tension == "HIGH"


# ---------------------------------------------------------------------------
# Independent Dimensions: Skew + Tension Combos
# ---------------------------------------------------------------------------

def test_left_skew_high_tension():
    """LEFT + HIGH = crash hedging scenario."""
    c = IVShapeClassifier()
    # First call for baseline
    c.classify(atm_iv=0.15, otm_put_iv=0.17, otm_call_iv=0.17, underlying_price=34000)
    # Second call: put wing jumped, atm also jumped
    r = c.classify(atm_iv=0.19, otm_put_iv=0.30, otm_call_iv=0.20, underlying_price=34000)
    assert r.directional_skew == "LEFT"
    assert r.tension == "HIGH"
    assert r.confidence > 0.5


def test_symmetric_high_tension():
    """SYMMETRIC + HIGH = universal panic (legacy would call PARALLEL)."""
    c = IVShapeClassifier()
    c.classify(atm_iv=0.15, otm_put_iv=0.17, otm_call_iv=0.17, underlying_price=34000)
    r = c.classify(atm_iv=0.20, otm_put_iv=0.22, otm_call_iv=0.22, underlying_price=34000)
    assert r.directional_skew == "SYMMETRIC"  # both wings lifted equally
    assert r.tension == "HIGH"                # big parallel shift
    # Legacy compat
    assert r.shape == "PARALLEL"


def test_right_skew_medium_tension():
    """RIGHT + MEDIUM = euphoria with moderate IV expansion."""
    c = IVShapeClassifier()
    c.classify(atm_iv=0.15, otm_put_iv=0.17, otm_call_iv=0.17, underlying_price=34000)
    r = c.classify(atm_iv=0.165, otm_put_iv=0.17, otm_call_iv=0.23, underlying_price=34000)
    assert r.directional_skew == "RIGHT"
    assert r.tension == "MEDIUM"


# ---------------------------------------------------------------------------
# Bounded Range
# ---------------------------------------------------------------------------

def test_slope_ratio_bounded():
    """slope_ratio is always in [-1, 1]."""
    c = IVShapeClassifier()
    extremes = [
        (0.15, 0.50, 0.15),    # extreme left
        (0.15, 0.15, 0.50),    # extreme right
        (0.15, 0.15, 0.15),    # flat
        (0.15, 0.01, 0.50),    # put below ATM
    ]
    for atm, put, call in extremes:
        c2 = IVShapeClassifier()
        r = c2.classify(atm_iv=atm, otm_put_iv=put, otm_call_iv=call, underlying_price=34000)
        assert -1.0 <= r.slope_ratio <= 1.0, (
            f"slope_ratio={r.slope_ratio} out of bounds for ({atm}, {put}, {call})"
        )


# ---------------------------------------------------------------------------
# Velocity Tracking
# ---------------------------------------------------------------------------

def test_delta_slope_ratio():
    """delta_slope_ratio tracks change between snapshots."""
    c = IVShapeClassifier()
    # T1: moderate left-skew
    c.classify(atm_iv=0.15, otm_put_iv=0.19, otm_call_iv=0.17, underlying_price=34000)
    # T2: stronger left-skew
    r2 = c.classify(atm_iv=0.15, otm_put_iv=0.23, otm_call_iv=0.17, underlying_price=34000)
    # Moving further left -> delta negative (or near zero if T1 was neutral)
    assert r2.delta_slope_ratio < 0 or abs(r2.delta_slope_ratio) < 1e-10


def test_delta_slope_ratio_reversal():
    """delta_slope_ratio shows reversal from LEFT to RIGHT."""
    c = IVShapeClassifier()
    c.classify(atm_iv=0.15, otm_put_iv=0.21, otm_call_iv=0.17, underlying_price=34000)
    r2 = c.classify(atm_iv=0.15, otm_put_iv=0.17, otm_call_iv=0.21, underlying_price=34000)
    assert r2.delta_slope_ratio > 0


# ---------------------------------------------------------------------------
# UNKNOWN / Edge Cases
# ---------------------------------------------------------------------------

def test_unknown_zero_iv():
    """Zero IV values -> UNKNOWN."""
    c = IVShapeClassifier()
    r = c.classify(atm_iv=0.0, otm_put_iv=0.0, otm_call_iv=0.0, underlying_price=34000)
    assert r.directional_skew == "UNKNOWN"
    assert r.tension == "UNKNOWN"


def test_unknown_partial():
    """Partial missing data -> UNKNOWN."""
    c = IVShapeClassifier()
    r = c.classify(atm_iv=0.15, otm_put_iv=0.0, otm_call_iv=0.15, underlying_price=34000)
    assert r.directional_skew == "UNKNOWN"


def test_low_confidence_force_default():
    """Very low confidence forces SYMMETRIC + LOW."""
    c = IVShapeClassifier()
    r = c.classify(atm_iv=0.15, otm_put_iv=0.16, otm_call_iv=0.16, underlying_price=34000)
    # Tiny slopes, first call (no prev) -> low confidence
    assert r.directional_skew == "SYMMETRIC"
    assert r.tension == "LOW"


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def test_to_dict_has_new_keys():
    """to_dict() includes new dimensions."""
    c = IVShapeClassifier()
    r = c.classify(atm_iv=0.15, otm_put_iv=0.25, otm_call_iv=0.15, underlying_price=34000)
    d = r.to_dict()
    assert "directional_skew" in d
    assert "tension" in d
    assert "shape" in d  # legacy compat


def test_to_dict_legacy_backward():
    """to_dict() still has legacy 'shape' and 'vol_regime' keys."""
    c = IVShapeClassifier()
    r = c.classify(atm_iv=0.15, otm_put_iv=0.25, otm_call_iv=0.15, underlying_price=34000)
    d = r.to_dict()
    assert d["shape"] == "LEFT_SKEW"
    assert "vol_regime" in d


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------

def test_reset_clears_velocity():
    """After reset, first classification has zero delta."""
    c = IVShapeClassifier()
    c.classify(atm_iv=0.15, otm_put_iv=0.25, otm_call_iv=0.15, underlying_price=34000)
    c.reset()
    r = c.classify(atm_iv=0.15, otm_put_iv=0.25, otm_call_iv=0.15, underlying_price=34000)
    assert r.delta_slope_ratio == 0.0
    assert r.atm_iv_change == 0.0


# ---------------------------------------------------------------------------
# Confidence floor
# ---------------------------------------------------------------------------

def test_confidence_scale():
    """Confidence scales with slope magnitude."""
    c1 = IVShapeClassifier()
    r1 = c1.classify(atm_iv=0.15, otm_put_iv=0.21, otm_call_iv=0.17, underlying_price=34000)
    c2 = IVShapeClassifier()
    r2 = c2.classify(atm_iv=0.15, otm_put_iv=0.27, otm_call_iv=0.17, underlying_price=34000)
    assert r1.confidence < r2.confidence
