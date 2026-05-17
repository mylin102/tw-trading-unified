"""
Tests for core/derivatives/iv_calculator.py

Verifies:
- Known IV test cases (CALL and PUT)
- Edge cases: expired, deep ITM, zero premium
- batch_iv helper
- Bounded output range
"""

import math
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.derivatives.iv_calculator import (
    iv_from_price,
    batch_iv,
    _bs_price,
    MIN_IV,
    MAX_IV,
    CONVERGENCE,
)


# ---------------------------------------------------------------------------
# Known test cases (computed externally or from market data)
# ---------------------------------------------------------------------------

def _approx_equal(a, b, tol=0.02):
    """Check if IV values are close (2% tolerance is fine for IV)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return abs(a - b) < tol


def test_known_call_iv():
    """ATM CALL with known parameters → expected IV ≈ 15%."""
    # S=17000, K=17000, T=30d, premium≈369 => IV≈15%
    # Premium ~ S * sigma * sqrt(T/(2*pi)) for ATM approx
    S, K, T_days, prem = 17000.0, 17000.0, 30, 369.0
    iv = iv_from_price("CALL", K, prem, S, T_days)
    assert iv is not None, "Should return IV"
    assert abs(iv - 0.15) < 0.05, f"Expected ~0.15 IV, got {iv}"


def test_known_put_iv():
    """ATM PUT symmetric to CALL."""
    S, K, T_days, prem = 17000.0, 17000.0, 30, 369.0
    iv_call = iv_from_price("CALL", K, prem, S, T_days)
    iv_put = iv_from_price("PUT", K, prem, S, T_days)
    assert iv_call is not None
    assert iv_put is not None
    assert abs(iv_call - iv_put) < 0.02, (
        f"ATM call/put IV should be similar: call={iv_call:.4f} put={iv_put:.4f}"
    )


def test_otm_call_lower_iv():
    """OTM CALL has lower premium → lower computed IV (before skew)."""
    S = 17000.0
    K_otm = 17300.0  # 300 points OTM
    T_days = 30
    # ATM premium is 369; OTM 17300 call should be cheaper
    otm_prem = 120.0  # plausible for 300 OTM, 30 DTE, 15% vol
    iv = iv_from_price("CALL", K_otm, otm_prem, S, T_days)
    assert iv is not None
    # Should be 15-25% territory
    assert 0.08 < iv < 0.30, f"OTM call IV out of range: {iv}"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

def test_expired_contract():
    """Expired (dte=0) → intrinsic value only."""
    S, K = 17000.0, 17000.0
    # ATM intrinsic = 0 for both sides
    iv = iv_from_price("CALL", K, 0.01, S, 0)
    # Premium barely above intrinsic → low IV, but not None
    # This is a degenerate case
    assert iv is not None
    assert iv > MIN_IV


def test_deep_itm_call():
    """Deep ITM CALL: premium >> intrinsic, should return valid IV."""
    S = 35000.0
    K = 33000.0  # 2000 ITM
    T_days = 21
    # Intrinsic = 2000. Premium might be 2100 (100 time value)
    prem = 2100.0
    iv = iv_from_price("CALL", K, prem, S, T_days)
    assert iv is not None
    # Time value of 100 on 2000 intrinsic → low IV
    assert 0.001 < iv < 0.50, f"Deep ITM IV unreasonable: {iv}"


def test_zero_premium():
    """Zero premium → None."""
    iv = iv_from_price("CALL", 17000.0, 0.0, 17000.0, 30)
    assert iv is None


def test_invalid_option_type():
    """Invalid option type → None."""
    iv = iv_from_price("STRADDLE", 17000.0, 100.0, 17000.0, 30)
    assert iv is None


def test_high_vol_scenario():
    """High vol (60%) scenario — verify bisection converges."""
    S = 17000.0
    K = 17000.0
    T_days = 30
    # Compute price at 60% IV, then solve back
    target_iv = 0.60
    price = _bs_price("CALL", S, K, T_days / 365.0, 0.02, target_iv)
    solved_iv = iv_from_price("CALL", K, price, S, T_days)
    assert solved_iv is not None
    assert abs(solved_iv - target_iv) < 0.02, (
        f"Solved IV {solved_iv:.4f} != target {target_iv:.4f}"
    )


def test_extreme_deep_otm():
    """Deep OTM (strike 2000 points away) with tiny premium."""
    S = 17000.0
    K = 19000.0  # 2000 OTM CALL
    T_days = 30
    prem = 5.0  # very cheap
    iv = iv_from_price("CALL", K, prem, S, T_days)
    # This should still converge — premium is small but option has some value
    assert iv is not None
    assert iv <= MAX_IV


# ---------------------------------------------------------------------------
# batch_iv
# ---------------------------------------------------------------------------

def test_batch_iv():
    """batch_iv returns IV for all quotes."""
    quotes = [
        {"option_type": "CALL", "strike": 17000.0, "mid": 369.0, "dte": 30},
        {"option_type": "PUT", "strike": 17000.0, "mid": 369.0, "dte": 30},
        {"option_type": "CALL", "strike": 17300.0, "mid": 80.0, "dte": 30},
    ]
    results = batch_iv(quotes, underlying_price=17000.0)
    assert len(results) == 3
    for r in results:
        assert "iv" in r
        assert r["iv"] is not None


def test_batch_iv_mixed_failure():
    """batch_iv handles a quote that would fail gracefully."""
    quotes = [
        {"option_type": "CALL", "strike": 17000.0, "mid": 369.0, "dte": 30},
        {"option_type": "CALL", "strike": 17000.0, "mid": 0.0, "dte": 30},  # zero → None
    ]
    results = batch_iv(quotes, underlying_price=17000.0)
    assert results[0]["iv"] is not None
    assert results[1]["iv"] is None


# ---------------------------------------------------------------------------
# Bounded range guarantee
# ---------------------------------------------------------------------------

def test_slope_ratio_range():
    """
    Verify the normalized formula used by shape_classifier stays bounded.

    This is a contract test for the algorithm the user approved:
      slope_ratio = (call_slope - put_slope) / (abs(call_slope) + abs(put_slope) + 1e-10)
    """
    def slope_ratio(put_slope, call_slope, eps=1e-10):
        return (call_slope - put_slope) / (abs(call_slope) + abs(put_slope) + eps)

    # Extreme cases
    assert abs(slope_ratio(0.10, 0.0)) <= 1.0, "Should be bounded"
    assert abs(slope_ratio(0.0, 0.10)) <= 1.0, "Should be bounded"
    assert abs(slope_ratio(0.0, 0.0)) <= 1.0, "Zero division handled"
    assert abs(slope_ratio(-1.0, 100.0)) <= 1.0, "Should be bounded"
    assert abs(slope_ratio(100.0, -1.0)) <= 1.0, "Should be bounded"

    # Known values
    assert slope_ratio(0.05, 0.01) < 0  # put steeper → negative
    assert slope_ratio(0.01, 0.05) > 0  # call steeper → positive
    assert abs(slope_ratio(0.0, 0.0)) < 0.001  # both flat → near zero


# ---------------------------------------------------------------------------
# Round-trip precision
# ---------------------------------------------------------------------------

def test_round_trip_precision():
    """Price → IV → price should reconstruct within tolerance."""
    S = 34000.0
    K = 34000.0  # ATM
    T_days = 14
    target_iv = 0.18

    price = _bs_price("CALL", S, K, T_days / 365.0, 0.02, target_iv)
    solved_iv = iv_from_price("CALL", K, price, S, T_days)
    reconstructed = _bs_price("CALL", S, K, T_days / 365.0, 0.02, solved_iv)

    assert abs(reconstructed - price) < 1.0, (
        f"Round trip error > 1 pt: {reconstructed} vs {price}"
    )
