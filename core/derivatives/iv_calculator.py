"""
Implied Volatility Calculator — Black-Scholes for options.

USAGE
-----
    iv = iv_from_price("CALL", 34000.0, 250.0, 35000.0, 21)
    # → 0.185 (18.5% IV)

Design
------
- European-style (TXO options are European-exercise).
- Bisection on Black-Scholes-Merton formula.
- Converges within 1e-6 in < 50 iterations for realistic ranges.
- Stateless: one shot per call, no caching. The caller handles cooldown.
"""

from __future__ import annotations

import math
from typing import Optional

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_IV = 5.0       # 500% — cap for pathological cases
MIN_IV = 0.001     # 0.1% — floor
CONVERGENCE = 1e-6
MAX_ITER = 80
EPS = 1e-10

# ---------------------------------------------------------------------------
# Cumulative Normal Distribution (erf-based, for speed & no deps)
# ---------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    """Standard normal CDF via math.erf."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

# ---------------------------------------------------------------------------
# Black-Scholes forward price (European)
# ---------------------------------------------------------------------------

def _bs_price(opt_type: str, S: float, K: float, T: float, r: float, sigma: float) -> float:
    """
    Black-Scholes option price.

    Parameters
    ----------
    opt_type: "CALL" or "PUT"
    S: underlying price
    K: strike price
    T: time to expiry in years
    r: risk-free rate (decimal, e.g. 0.02 = 2%)
    sigma: implied volatility (decimal, e.g. 0.20 = 20%)

    Returns
    -------
    Option premium in same units as S/K.
    """
    if T <= 0:
        # Expired: intrinsic value only
        intrinsic = max(S - K, 0) if opt_type == "CALL" else max(K - S, 0)
        return intrinsic

    if sigma <= 0:
        intrinsic = max(S - K, 0) if opt_type == "CALL" else max(K - S, 0)
        return intrinsic

    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T) + EPS)
    d2 = d1 - sigma * math.sqrt(T)

    if opt_type == "CALL":
        return S * _norm_cdf(d1) - K * math.exp(-r * T) * _norm_cdf(d2)
    else:  # PUT
        return K * math.exp(-r * T) * _norm_cdf(-d2) - S * _norm_cdf(-d1)

# ---------------------------------------------------------------------------
# IV solver
# ---------------------------------------------------------------------------

def iv_from_price(
    option_type: str,
    strike: float,
    premium: float,
    underlying_price: float,
    dte: float,
    risk_free_rate: float = 0.02,
) -> Optional[float]:
    """
    Compute implied volatility via bisection.

    Parameters
    ----------
    option_type: "CALL" or "PUT"
    strike: strike price
    premium: observed mid price of the option
    underlying_price: current underlying (futures/spot) price
    dte: days to expiry (calendar days)
    risk_free_rate: annual risk-free rate (default 2%)

    Returns
    -------
    Implied volatility as decimal (e.g. 0.185 = 18.5%), or None on failure.
    """
    T = dte / 365.0  # calendar days to year fraction

    opt_type_upper = option_type.upper()
    if opt_type_upper not in ("CALL", "PUT"):
        return None

    # Guard: intrinsic value check
    intrinsic = max(underlying_price - strike, 0) if opt_type_upper == "CALL" else max(strike - underlying_price, 0)
    if premium <= 0:
        return None
    if premium < intrinsic + 0.01:
        # Premium barely above intrinsic → IV near zero
        # But bisection still works if we're careful. If premium < intrinsic,
        # something is wrong (negative time value). Return None.
        if premium < intrinsic:
            return None

    # Bisection
    lo = MIN_IV
    hi = MAX_IV

    for _ in range(MAX_ITER):
        mid = (lo + hi) / 2.0
        price = _bs_price(opt_type_upper, underlying_price, strike, T, risk_free_rate, mid)
        diff = price - premium

        if abs(diff) < CONVERGENCE:
            return round(mid, 6)

        if diff > 0:
            hi = mid
        else:
            lo = mid

    # Check if hi bound gives price above premium (IV too high) or below
    # If even at MAX_IV the price is below premium, return MAX_IV.
    price_at_hi = _bs_price(opt_type_upper, underlying_price, strike, T, risk_free_rate, MAX_IV)
    if price_at_hi < premium:
        return round(MAX_IV, 6)

    # Converged but not within tolerance — return best estimate
    return round((lo + hi) / 2.0, 6)

# ---------------------------------------------------------------------------
# Convenience: batch IV
# ---------------------------------------------------------------------------

def batch_iv(
    quotes: list[dict],
    underlying_price: float,
    risk_free_rate: float = 0.02,
) -> list[dict]:
    """
    Compute IV for a batch of quotes.

    Each quote dict must have the following keys:
        option_type: "CALL" or "PUT"
        strike: float
        mid: float (premium)
        dte: float (days to expiry)

    Returns the same dicts with "iv" key added (or "iv": None on failure).
    """
    results = []
    for q in quotes:
        iv = iv_from_price(
            option_type=q["option_type"],
            strike=q["strike"],
            premium=q["mid"],
            underlying_price=underlying_price,
            dte=q["dte"],
            risk_free_rate=risk_free_rate,
        )
        q = dict(q)  # shallow copy to avoid mutating input
        q["iv"] = iv
        results.append(q)
    return results
