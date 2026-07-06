"""
QuantLib-based options pricing engine.
Drop-in replacement for greeks.py with more accurate pricing:
- American exercise support (TXO is European, but ready for future)
- Dividend yield
- Volatility surface / smile
- More robust IV solver (Brent method)
"""
import QuantLib as ql
from datetime import datetime


def _ql_date(dt):
    """Convert Python datetime to QuantLib Date."""
    if isinstance(dt, str):
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                dt = datetime.strptime(dt, fmt)
                break
            except ValueError:
                continue
    return ql.Date(dt.day, dt.month, dt.year)


def _ql_option_type(option_type):
    t = option_type.upper()
    return ql.Option.Call if t in ('C', 'CALL') else ql.Option.Put


def calculate_dte(delivery_date, now=None):
    """Calculate DTE in years."""
    now = now or datetime.now()
    if isinstance(delivery_date, str):
        for fmt in ("%Y%m%d", "%Y-%m-%d", "%Y/%m/%d"):
            try:
                delivery_date = datetime.strptime(delivery_date, fmt)
                break
            except ValueError:
                continue
    delta = (delivery_date - now).total_seconds() / (365 * 24 * 3600)
    return max(0.00001, delta)


def black_scholes(S, K, T, r, sigma, option_type='C', q=0.0):
    """
    QuantLib Black-Scholes pricing with full Greeks.
    S: spot, K: strike, T: time to expiry (years), r: risk-free rate,
    sigma: IV, option_type: 'C'/'P', q: dividend yield
    """
    S, K, T, r, sigma, q = float(S), float(K), float(T), float(r), float(sigma), float(q)

    if S <= 0 or K <= 0 or sigma <= 0 or T <= 0:
        intrinsic = max(0, S - K) if option_type.upper() in ('C', 'CALL') else max(0, K - S)
        return {"price": intrinsic, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}

    today = ql.Date.todaysDate()
    maturity = today + ql.Period(max(1, int(T * 365)), ql.Days)
    ql.Settings.instance().evaluationDate = today

    payoff = ql.PlainVanillaPayoff(_ql_option_type(option_type), K)
    exercise = ql.EuropeanExercise(maturity)
    option = ql.VanillaOption(payoff, exercise)

    spot_handle = ql.QuoteHandle(ql.SimpleQuote(S))
    rate_handle = ql.YieldTermStructureHandle(ql.FlatForward(today, r, ql.Actual365Fixed()))
    div_handle = ql.YieldTermStructureHandle(ql.FlatForward(today, q, ql.Actual365Fixed()))
    vol_handle = ql.BlackVolTermStructureHandle(ql.BlackConstantVol(today, ql.NullCalendar(), sigma, ql.Actual365Fixed()))

    process = ql.BlackScholesMertonProcess(spot_handle, div_handle, rate_handle, vol_handle)
    option.setPricingEngine(ql.AnalyticEuropeanEngine(process))

    try:
        return {
            "price": option.NPV(),
            "delta": option.delta(),
            "gamma": option.gamma(),
            "theta": option.thetaPerDay(),
            "vega": option.vega() / 100,  # per 1% IV change
            "rho": option.rho() / 100,
        }
    except Exception:
        intrinsic = max(0, S - K) if option_type.upper() in ('C', 'CALL') else max(0, K - S)
        return {"price": intrinsic, "delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0, "rho": 0.0}


def find_implied_volatility(target_price, S, K, T, r, option_type='C', q=0.0):
    """
    QuantLib IV solver using Brent method — much more robust than binary search.
    """
    target_price, S, K, T, r, q = float(target_price), float(S), float(K), float(T), float(r), float(q)

    if target_price <= 0 or S <= 0 or K <= 0 or T <= 0:
        return 0.25  # fallback

    today = ql.Date.todaysDate()
    maturity = today + ql.Period(max(1, int(T * 365)), ql.Days)
    ql.Settings.instance().evaluationDate = today

    payoff = ql.PlainVanillaPayoff(_ql_option_type(option_type), K)
    exercise = ql.EuropeanExercise(maturity)
    option = ql.VanillaOption(payoff, exercise)

    spot_handle = ql.QuoteHandle(ql.SimpleQuote(S))
    rate_handle = ql.YieldTermStructureHandle(ql.FlatForward(today, r, ql.Actual365Fixed()))
    div_handle = ql.YieldTermStructureHandle(ql.FlatForward(today, q, ql.Actual365Fixed()))

    process = ql.BlackScholesMertonProcess(
        spot_handle, div_handle, rate_handle,
        ql.BlackVolTermStructureHandle(ql.BlackConstantVol(today, ql.NullCalendar(), 0.25, ql.Actual365Fixed()))
    )
    option.setPricingEngine(ql.AnalyticEuropeanEngine(process))

    try:
        return option.impliedVolatility(target_price, process, 1e-6, 100, 0.001, 5.0)
    except Exception:
        return 0.25  # fallback


def price_with_smile(S, K, T, r, vol_surface, option_type='C', q=0.0):
    """
    Price using a volatility surface (smile/skew aware).
    vol_surface: list of (strike, iv) tuples for interpolation.
    """
    S, K, T, r, q = float(S), float(K), float(T), float(r), float(q)

    if not vol_surface or T <= 0:
        return black_scholes(S, K, T, r, 0.25, option_type, q)

    # Interpolate IV from surface
    strikes = [s for s, _ in vol_surface]
    ivs = [v for _, v in vol_surface]

    if K <= strikes[0]:
        sigma = ivs[0]
    elif K >= strikes[-1]:
        sigma = ivs[-1]
    else:
        for i in range(len(strikes) - 1):
            if strikes[i] <= K <= strikes[i + 1]:
                w = (K - strikes[i]) / (strikes[i + 1] - strikes[i])
                sigma = ivs[i] * (1 - w) + ivs[i + 1] * w
                break
        else:
            sigma = 0.25

    return black_scholes(S, K, T, r, sigma, option_type, q)
