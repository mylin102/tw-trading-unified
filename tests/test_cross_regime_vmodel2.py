import pandas as pd
import numpy as np

from strategies.cross_regime import RegimeDetector, TMFLocalDetector, CrossRegimeEngine


def make_bars_from_prices(prices, vol=10, end_ts=None, freq='1min'):
    if end_ts is None:
        end_ts = pd.Timestamp.now()
    timestamps = pd.date_range(end=end_ts, periods=len(prices), freq=freq)
    bars = []
    for p, t in zip(prices, timestamps):
        bars.append({
            "ts": t,
            "open": float(p),
            "high": float(p) + 0.5,
            "low": float(p) - 0.5,
            "close": float(p),
            "volume": vol,
        })
    return bars


def test_regime_detector_trend_and_low_vol():
    det = RegimeDetector()
    # Trend up by using slope_trend very small to force detection
    prices = [100 + i * 5 for i in range(25)]
    bars = make_bars_from_prices(prices)
    assert det.detect(bars, slope_trend=0.001, low_vol_th=0.1) == "TREND_UP"

    # Low volume detection
    prices2 = [100 + (i % 3) * 0.01 for i in range(25)]
    bars2 = make_bars_from_prices(prices2)
    assert det.detect(bars2, slope_trend=0.8, low_vol_th=100.0) == "LOW_VOL"


def test_tmf_local_detector_breakout_and_stalled():
    det = TMFLocalDetector()
    prices = list(100 + np.linspace(0, 5, 25))
    bars = make_bars_from_prices(prices)
    # enlarge per-bar range so detector does not treat series as STALLED
    for b in bars:
        b["high"] = b["close"] + 6.0
        b["low"] = b["close"] - 6.0
    bars[-1]["close"] = float(max([b["high"] for b in bars[-5:]])) * 1.0001
    assert det.detect(bars) == "BREAKOUT_READY"

    det2 = TMFLocalDetector()
    prices2 = [100.0 for _ in range(25)]
    bars2 = make_bars_from_prices(prices2)
    assert det2.detect(bars2) == "STALLED"


def test_cross_engine_decisions_and_freshness():
    engine = CrossRegimeEngine()
    # Freshness blocking not part of this engine (older variant) — check decision matrix
    res = engine.decide("TREND_UP", "BREAKOUT_READY")
    assert isinstance(res, dict)
    assert res.get("allow_trade") is True

    res2 = engine.decide("UNKNOWN", "BREAKOUT_READY")
    assert res2.get("allow_trade") is False
