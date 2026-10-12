# 2026-10-12 Codex: separate PAPER exit liquidity policy from entry width.
from datetime import datetime, timedelta

import pytest

from core.strategy_context import StrategyContext, MarketData, PositionView
from tests.strategies.test_mts_crossday_policy_j import held_paper


def run_release(held_paper, monkeypatch, *, mode="paper_active", exit_cap=None, age=100.0, wide=True):
    s, bar, cfg, events = held_paper
    # Isolate the width gate, not a proposed runtime trend-gate bypass.
    monkeypatch.setenv("MTS_ATR_RELEASE_TREND_GATE", "0")
    s._hvwap_execution_mode = mode
    s._entry_ts = datetime.now() - timedelta(minutes=5)
    s._entry_guard_start_ms = s._entry_ts.timestamp() * 1000
    s._entry_ts_ms = s._entry_guard_start_ms
    cfg["params"].update(release_stop_points=20, atr_multiplier_stop=2,
                         confirm_ticks=1, confirm_ms=0)
    if exit_cap is not None:
        cfg["params"]["paper_exit_max_spread_width"] = exit_cap
    bar.update(near_close=49900.0, far_close=50085.0, atr=10.0,
               near_bid=49898.0, near_ask=49903.0,
               far_bid=50080.0, far_ask=50090.0,
               near_tick_age_ms=age, far_tick_age_ms=age)
    if not wide:
        bar.update(near_bid=49899.0, near_ask=49901.0,
                   far_bid=50084.0, far_ask=50086.0)
    return s, s.on_bar(StrategyContext(
        market=MarketData(last_bar=bar, ticker="TMF"),
        position=PositionView(size=2), config=cfg))


def test_paper_risk_release_not_stranded_by_entry_width(held_paper, monkeypatch):
    s, signal = run_release(held_paper, monkeypatch)
    assert signal is not None
    assert signal.action == "PARTIAL_EXIT"
    assert "RELEASE_NEAR" in signal.reason
    assert s._max_spread_width == 4


def test_paper_exit_has_independent_optional_width_cap(held_paper, monkeypatch):
    s, signal = run_release(held_paper, monkeypatch, exit_cap=4)
    assert signal is None
    assert s._lifecycle == "OPEN"


def test_live_keeps_existing_width_cap(held_paper, monkeypatch):
    s, signal = run_release(held_paper, monkeypatch, mode="live_ready")
    assert signal is None
    assert s._lifecycle == "OPEN"


def test_paper_emergency_cannot_use_stale_quote(held_paper, monkeypatch):
    s, signal = run_release(held_paper, monkeypatch, age=60000.0, wide=False)
    assert signal is None
    assert s._lifecycle == "OPEN"
