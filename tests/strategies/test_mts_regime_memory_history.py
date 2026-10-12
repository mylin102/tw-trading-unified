# 2026-10-12 Codex: reproduce CSV-less regime warmup through the monitor seam.
from collections import deque

import pandas as pd

from strategies.futures.monitor import FuturesMonitor
from strategies.plugins.futures.active.mts_hvwap_candidate import (
    aggregate_completed_bars, classify_60m_regime, Regime60m,
)


def test_memory_history_warms_60m_without_indicator_csv(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("TRADING_FUTURES_MODE", "paper")
    monitor = FuturesMonitor.__new__(FuturesMonitor)
    monitor.live_trading = False
    monitor.ticker = "TMF"
    timestamps = pd.date_range("2026-10-12 08:45", "2026-10-12 11:45", freq="5min")
    monitor._tick_bars_deque = deque([
        {"ts": ts.timestamp(), "open": 49000 + i, "high": 49002 + i,
         "low": 48999 + i, "close": 49001 + i, "volume": 10}
        for i, ts in enumerate(timestamps)
    ])
    decision = pd.Timestamp("2026-10-12 11:45")
    bars = monitor._get_hvwap_regime_prewarm_bars(decision)
    assert bars, "real current-session K bars must not depend on indicator CSV"
    assert max(float(b["ts"]) for b in bars) <= decision.timestamp()
    # The 11:45 bucket has not closed; only its predecessors are evidence.
    assert len(bars) == len(timestamps) - 1
    hours = aggregate_completed_bars(bars, decision, 3600)
    assert len(hours) == 2
    assert classify_60m_regime(hours) is not Regime60m.UNKNOWN
    # A later decision must refresh the source, not reuse an empty/session-only cache.
    later = monitor._get_hvwap_regime_prewarm_bars(pd.Timestamp("2026-10-12 11:50"))
    assert len(later) == len(timestamps)
