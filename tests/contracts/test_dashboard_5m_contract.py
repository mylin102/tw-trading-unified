import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

from core.futures_indicator_contract import (
    CORE_COLUMNS,
    FUTURES_INDICATOR_COLUMNS,
    ordered_indicator_columns,
)
from strategies.futures.monitor import FuturesMonitor, _entry_bbo_view


def test_contract_puts_entry_columns_first_and_preserves_unknowns_last():
    assert list(FUTURES_INDICATOR_COLUMNS[:18]) == [
        "timestamp", "near_bid", "near_ask", "near_bidask_age_ms",
        "near_entry_bbo_state", "far_bid", "far_ask", "far_bidask_age_ms",
        "far_entry_bbo_state", "close", "spread", "spread_z", "sqz_on",
        "momentum", "atr_used", "vwap", "regime", "session",
    ]
    assert ordered_indicator_columns(["unknown", "spread_z", "timestamp"])[0:2] == [
        "timestamp", "spread_z"
    ]
    assert ordered_indicator_columns(["unknown", "spread_z", "timestamp"])[-1] == "unknown"


def test_save_bar_uses_bidask_slots_for_quote_columns(tmp_path, monkeypatch):
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.dry_run = False
    mon.live_trading = False
    mon.ticker = "TMF"
    mon.market_data = {
        "TMF": {"bid": 48351.0, "ask": 48353.0, "bidask_at": 1000.0},
        "TMF_FAR": {"bid": 48502.0, "ask": 48512.0, "bidask_at": 1000.0},
    }
    mon._indicator_cols = None
    mon._indicators_migrated = True
    mon._last_saved_ts = None
    monkeypatch.setattr("strategies.futures.monitor.runtime_logs", lambda *parts: str(tmp_path))
    monkeypatch.setattr("strategies.futures.monitor.time", SimpleNamespace(time=lambda: 1001.0))

    row = pd.Series({
        "Close": 48352.0, "spread_z": 2.5, "sqz_on": True,
        "momentum": 1.0, "atr_used": 72.5, "vwap": 48300.0,
    }, name=pd.Timestamp("2026-09-23 10:00:00"))
    mon._save_bar(row, score=3.0, regime="NORMAL")
    path = next(Path(tmp_path).glob("TMF_*_PAPER_indicators.csv"))
    frame = pd.read_csv(path)
    assert list(frame.columns[:18]) == list(CORE_COLUMNS)
    record = frame.iloc[-1]
    assert record["near_bid"] == 48351.0
    assert record["far_ask"] == 48512.0
    assert record["near_entry_bbo_state"] == "OK"
    assert record["far_entry_bbo_state"] == "OK"


def test_save_bar_missing_slot_never_derives_quote_from_close(tmp_path, monkeypatch):
    mon = FuturesMonitor.__new__(FuturesMonitor)
    mon.dry_run = False
    mon.live_trading = False
    mon.ticker = "TMF"
    mon.market_data = {"TMF": {}, "TMF_FAR": {}}
    mon._indicator_cols = None
    mon._indicators_migrated = True
    mon._last_saved_ts = None
    monkeypatch.setattr("strategies.futures.monitor.runtime_logs", lambda *parts: str(tmp_path))
    monkeypatch.setattr("strategies.futures.monitor.time", SimpleNamespace(time=lambda: 1001.0))
    row = pd.Series({"Close": 48352.0}, name=pd.Timestamp("2026-09-23 10:00:00"))
    mon._save_bar(row, score=0.0, regime="NORMAL")
    frame = pd.read_csv(next(Path(tmp_path).glob("TMF_*_PAPER_indicators.csv")))
    record = frame.iloc[-1]
    assert pd.isna(record["near_bid"])
    assert pd.isna(record["far_ask"])
    assert record["near_entry_bbo_state"] == "NO_BIDASK"
