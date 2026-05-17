"""
Tests for core/derivatives/iv_percentile.py
"""

import datetime
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.derivatives.iv_percentile import IVPercentileEngine


def _ts(offset_sec=0):
    return datetime.datetime.utcnow() - datetime.timedelta(seconds=offset_sec)


W = 3600  # default window_sec


def test_basic_percentile():
    engine = IVPercentileEngine(window_sec=W, min_samples=10)
    for i in range(21):
        engine.record(0.10 + i * 0.01, timestamp=_ts(W - 60))
    result = engine.get_percentile(0.20)
    assert result["ready"]
    assert 0.30 < result["iv_percentile"] < 0.70
    assert result["sample_count"] == 21


def test_zscore_computation():
    engine = IVPercentileEngine(window_sec=W, min_samples=5)
    for _ in range(19):
        engine.record(0.15, timestamp=_ts(W - 600))
    engine.record(0.30, timestamp=_ts(0))
    result = engine.get_percentile(0.30)
    assert result["ready"]
    assert result["iv_zscore"] > 1.0


def test_zscore_near_zero_when_std_tiny():
    engine = IVPercentileEngine(window_sec=W, min_samples=5)
    for _ in range(10):
        engine.record(0.15, timestamp=_ts(W - 600))
    result = engine.get_percentile(0.15)
    assert result["iv_zscore"] == 0.0


def test_not_ready_below_min_samples():
    engine = IVPercentileEngine(window_sec=W, min_samples=50)
    for _ in range(10):
        engine.record(0.15, timestamp=_ts(W - 600))
    result = engine.get_percentile(0.15)
    assert not result["ready"]
    assert result["sample_count"] == 10


def test_ready_at_min_samples():
    engine = IVPercentileEngine(window_sec=7200, min_samples=10)
    for i in range(10):
        engine.record(0.10 + i * 0.01, timestamp=_ts(3600 - i * 300))
    result = engine.get_percentile(0.15)
    assert result["ready"]


def test_stale_samples_pruned():
    engine = IVPercentileEngine(window_sec=60, min_samples=2)
    for _ in range(5):
        engine.record(0.20, timestamp=_ts(600))  # 10 min old -> stale (window=60s)
    engine.record(0.15, timestamp=_ts(0))
    result = engine.get_percentile(0.15)
    assert result["sample_count"] == 1


def test_prune_on_every_query():
    engine = IVPercentileEngine(window_sec=120, min_samples=1)
    engine.record(0.15, timestamp=_ts(60))    # 60s old -> within
    engine.record(0.20, timestamp=_ts(600))   # 600s old -> stale
    result = engine.get_percentile(0.20)
    assert result["sample_count"] == 1


def test_prune_correctly():
    engine = IVPercentileEngine(window_sec=1800, min_samples=1)
    engine.record(0.15, timestamp=_ts(600))   # 600s = 10min -> within
    engine.record(0.20, timestamp=_ts(2400))  # 2400s = 40min -> stale
    result = engine.get_percentile(0.25)
    assert result["sample_count"] == 1


def test_zero_iv_rejected():
    engine = IVPercentileEngine(window_sec=W, min_samples=1)
    engine.record(0.0)
    assert engine.sample_count == 0


def test_negative_iv_rejected():
    engine = IVPercentileEngine(window_sec=W, min_samples=1)
    engine.record(-0.05)
    assert engine.sample_count == 0


def test_none_iv_rejected():
    engine = IVPercentileEngine(window_sec=W, min_samples=1)
    engine.record(None)
    assert engine.sample_count == 0


def test_batch_record():
    engine = IVPercentileEngine(window_sec=W, min_samples=5)
    values = [(_ts(W - i * 300), 0.10 + i * 0.02) for i in range(10)]
    engine.record_batch(values)
    assert engine.sample_count == 10
    result = engine.get_percentile(0.20)
    assert result["ready"]


def test_batch_rejects_invalid():
    engine = IVPercentileEngine(window_sec=W, min_samples=1)
    values = [
        (_ts(W), 0.0),
        (_ts(W), -0.05),
        (_ts(W), 0.15),
    ]
    engine.record_batch(values)
    assert engine.sample_count == 1


def test_reset():
    engine = IVPercentileEngine(window_sec=W, min_samples=1)
    for i in range(10):
        engine.record(0.10 + i * 0.01, timestamp=_ts(W - 600))
    assert engine.sample_count == 10
    engine.reset()
    assert engine.sample_count == 0


def test_get_stats():
    engine = IVPercentileEngine(window_sec=W, min_samples=3)
    for i in range(5):
        engine.record(0.10 + i * 0.05, timestamp=_ts(W - i * 600))
    stats = engine.get_stats()
    assert stats["ready"]
    assert stats["sample_count"] == 5
    assert stats["mean_iv"] > 0
    assert stats["std_iv"] > 0
    assert stats["current_iv"] > 0


def test_get_stats_not_ready():
    engine = IVPercentileEngine(window_sec=W, min_samples=10)
    engine.record(0.15, timestamp=_ts(0))
    stats = engine.get_stats()
    assert not stats["ready"]


def test_percentile_clamp():
    engine = IVPercentileEngine(window_sec=W, min_samples=5)
    for i in range(10):
        engine.record(0.10 + i * 0.02, timestamp=_ts(W - i * 300))
    result = engine.get_percentile(0.50)
    assert 0 <= result["iv_percentile"] <= 1.0
    assert result["iv_percentile"] >= 0.9
    result = engine.get_percentile(0.01)
    assert 0 <= result["iv_percentile"] <= 1.0
    assert result["iv_percentile"] <= 0.1


def test_output_keys():
    engine = IVPercentileEngine(window_sec=W, min_samples=1)
    engine.record(0.15, timestamp=_ts(0))
    result = engine.get_percentile(0.15)
    expected_keys = {"atm_iv", "iv_percentile", "iv_zscore", "sample_count", "window_sec", "ready"}
    assert set(result.keys()) == expected_keys
