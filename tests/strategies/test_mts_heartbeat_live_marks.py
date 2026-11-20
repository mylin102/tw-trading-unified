"""2026-11-20 Hermes Agent: NEAR_PRICE_FIX at the real _strategy_tick callsite.

Incident evidence (Mini PAPER, 2026-11-19 night, 4 trades):
  221/237 Policy J evaluations carried near_price_source=BAR_CLOSE while
  far_price_source=LIVE_TICK. The heartbeat path (_strategy_tick) injected
  far_close_rt from _far_current_bar but never near_close_rt, so the near
  mark was the last closed 5m bar while the far mark was a live tick.
  The mismatched pair made the combined upl oscillate (+798 -> -152 within
  12 ms), 117/235 peak candidates were rejected as CANDIDATE_NORMALIZED,
  the durable peak was understated (228 vs ~798) and the giveback exit
  fired ~340 TWD late (4 trades: -966.6 TWD realized).

This test drives the real _strategy_tick() and asserts BOTH legs reach the
strategy bar with live marks, i.e. no BAR_CLOSE fallback on this path.
"""
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from strategies.futures import monitor as fm
from tests.core.test_mts_mode_data_isolation import isolated_runtime, _monitor


def _prepared_monitor(isolated_runtime, monkeypatch):
    import time

    mon = _monitor(isolated_runtime, monkeypatch)
    mon.cfg["mts"] = {"enabled": True}
    mon.dry_run = False
    mon.is_trading_ready = True
    mon.last_tick_at = time.time()
    mon.manual_trade_flag_path = str(isolated_runtime / "no-manual-flag")
    mon.contract = SimpleNamespace(delivery_date="2099/01/01")
    mon._is_settlement_day = lambda _: False
    mon._tmf_feed_age_secs = lambda: 0
    mon._trend_hold_active = lambda *args: False
    mon._scout_entry_bar = -1
    for name in ("_process_live_upl_refresh_command", "_reconcile_exit_only_authorization",
                 "_run_mts_watchdog", "_sync_mts_status", "_check_futures_contract_staleness",
                 "_refresh_runtime_status", "_check_tick_api_consistency", "_hourly_no_trade_audit",
                 "_update_mtf_snapshot"):
        setattr(mon, name, lambda *args, **kwargs: None)
    mon._check_canonical_freshness = lambda _: []
    mon._periodic_backfill_bars = lambda: None
    monkeypatch.setattr("core.date_utils.is_day_session", lambda _: True)
    monkeypatch.setattr("core.date_utils.is_night_session", lambda _: False)
    monkeypatch.setattr(fm, "get_taifex_futures_hhmm", lambda: 1000)
    monkeypatch.setattr(fm, "get_taifex_futures_session_type", lambda: "day")
    monkeypatch.setattr(fm, "calculate_futures_squeeze", lambda frame, **kwargs: frame)
    monkeypatch.setattr(fm, "attach_bar_metadata", lambda frame: frame)
    return mon


def _frame():
    now = datetime.now()
    row = {"Open": 44000.0, "High": 44050.0, "Low": 43950.0, "Close": 44000.0,
           "Volume": 1.0, "vwap": 44000.0, "atr": 50.0, "atr_raw": 50.0,
           "momentum": 0.0, "sqz_on": False,
           "trading_day": now.strftime("%Y-%m-%d")}
    return pd.DataFrame([row, row], index=pd.date_range(now, periods=2, freq="5min"))


def test_strategy_tick_injects_live_near_and_far_marks(isolated_runtime, monkeypatch):
    mon = _prepared_monitor(isolated_runtime, monkeypatch)
    captured = []
    mon._mts_tick = MagicMock(side_effect=lambda enriched_bar=None: captured.append(enriched_bar))
    # near live mark comes from market_data[self.ticker] (written on every
    # near-month primary tick), far live mark from _far_current_bar.
    mon.market_data[mon.ticker] = {"close": 44123.0, "bid": 44122.0, "ask": 44124.0}
    mon._far_current_bar = {"close": 44200.0, "high": 44210.0, "low": 44190.0}

    frame = _frame()
    mon._get_tick_bars_df = lambda: frame
    monkeypatch.setattr(fm, "build_preferred_canonical_bar_frames",
                        lambda *args, **kwargs: ({"5m": frame}, {"source": "tick-5m"}))

    mon._strategy_tick()

    assert captured, "_strategy_tick must have called _mts_tick"
    bar = captured[-1]
    assert bar["near_close_rt"] == 44123.0
    assert bar["far_close_rt"] == 44200.0
    # Both *_rt keys present => the strategy's downstream override
    # (tmf_spread/monitor: near_close = near_close_rt) can resolve LIVE_TICK
    # for BOTH legs. Before the fix only far_close_rt existed here, so the
    # near mark silently fell back to the last closed 5m bar.
    assert "near_close_rt" in bar and "far_close_rt" in bar


def test_strategy_tick_without_near_tick_does_not_invent_near_mark(isolated_runtime, monkeypatch):
    # Fail-closed: no near tick received yet -> no near_close_rt key, and the
    # bar must NOT be relabelled LIVE_TICK. (Far injection stays as-is.)
    mon = _prepared_monitor(isolated_runtime, monkeypatch)
    captured = []
    mon._mts_tick = MagicMock(side_effect=lambda enriched_bar=None: captured.append(enriched_bar))
    mon.market_data.pop(mon.ticker, None)
    mon._far_current_bar = {"close": 44200.0}

    frame = _frame()
    mon._get_tick_bars_df = lambda: frame
    monkeypatch.setattr(fm, "build_preferred_canonical_bar_frames",
                        lambda *args, **kwargs: ({"5m": frame}, {"source": "tick-5m"}))

    mon._strategy_tick()

    assert captured
    bar = captured[-1]
    assert "near_close_rt" not in bar
    assert bar["far_close_rt"] == 44200.0
