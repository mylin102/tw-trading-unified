import pytest
from strategies.adaptive_engine import AdaptiveEngine


def make_bars(closes, highs=None, lows=None):
    if highs is None:
        highs = [c + 1 for c in closes]
    if lows is None:
        lows = [c - 1 for c in closes]
    return [{"close": c, "high": h, "low": l} for c, h, l in zip(closes, highs, lows)]


def test_detect_regime_low_vol():
    closes = [100.0 + (i % 2) * 0.1 for i in range(20)]
    highs = [c + 0.5 for c in closes]
    lows = [c - 0.4 for c in closes]
    bars = make_bars(closes, highs, lows)
    ae = AdaptiveEngine()
    reg = ae.detect_regime(bars)
    assert reg == "LOW_VOL"


def test_detect_regime_trend():
    closes = [i * 1.0 for i in range(20)]
    # Make volatility sufficiently large so LOW_VOL guard does not trigger
    highs = [c + 10 for c in closes]
    lows = [c - 1 for c in closes]
    bars = make_bars(closes, highs, lows)
    ae = AdaptiveEngine()
    reg = ae.detect_regime(bars)
    assert reg == "TREND"


def test_detect_regime_mean_revert():
    # moderate volatility but little slope
    closes = [100 + ((-1) ** i) * (i % 3) for i in range(20)]
    highs = [c + 6 for c in closes]
    lows = [c - 5 for c in closes]
    bars = make_bars(closes, highs, lows)
    ae = AdaptiveEngine()
    reg = ae.detect_regime(bars)
    assert reg == "MEAN_REVERT"


def test_adjust_threshold_and_weights():
    ae = AdaptiveEngine()
    closes = list(range(100, 120))
    highs = [c + 20 for c in closes]
    lows = [c for c in closes]
    bars = make_bars(closes, highs, lows)
    orb, vwap = ae.adjust_threshold(0.6, 0.8, bars)
    assert isinstance(orb, float)
    assert isinstance(vwap, float)
    ow, vw = ae.strategy_weight()
    assert isinstance(ow, float) and isinstance(vw, float)
