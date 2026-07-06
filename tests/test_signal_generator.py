import pytest
import pandas as pd
import numpy as np
from backtest.signal_generator import generate_signals

def test_signal_generation_logic():
    """Verify that signal generator correctly maps strategy output to boolean arrays."""
    # Create mock data
    n = 100
    df = pd.DataFrame({
        "close": np.linspace(32000, 33000, n),
        "score": np.random.randint(0, 100, n),
        "bullish_align": [True] * n,
        "bearish_align": [False] * n,
        "atr": [30] * n,
        "sqz_on": [False] * n,
        "momentum": [50] * n,
        "mom_state": [3] * n,
        "fired": [False] * n,
        "mom_velo": [5] * n,
        "ema_filter": np.linspace(31900, 32900, n),
        "recent_high": np.linspace(32100, 33100, n),
        "recent_low": np.linspace(31900, 32900, n),
        "vwap": np.linspace(31950, 32950, n)
    })
    df["open"] = df["close"] - 5
    df["high"] = df["close"] + 10
    df["low"] = df["close"] - 10
    df["volume"] = 500
    # Map lowercase to uppercase for strategy compatibility
    df["Close"] = df["close"]
    df["High"] = df["high"]
    df["Low"] = df["low"]
    df["Open"] = df["open"]
    df["Volume"] = df["volume"]
    
    df.index = pd.date_range("2026-04-02 09:00", periods=n, freq="5min")
    
    cfg = {"strategy": {"regime_filter": "mid", "entry_score": 20}}
    
    # Test with a known strategy
    longs, shorts = generate_signals(df, "squeeze_breakout", cfg, warmup=60)
    
    assert len(longs) == n
    assert len(shorts) == n
    assert longs.dtype == bool
    assert shorts.dtype == bool
    # Warmup period should be all False
    assert not any(longs[:60])
    assert not any(shorts[:60])

def test_invalid_strategy():
    """Should raise ValueError for non-existent strategy."""
    df = pd.DataFrame({"close": [1, 2, 3]})
    with pytest.raises(ValueError):
        generate_signals(df, "non_existent_strategy", {})
