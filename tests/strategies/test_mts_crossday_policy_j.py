# 2026-10-12 Codex: real on_bar regression for legal multi-day PAPER inventory.
from datetime import datetime, timedelta

import pytest

from core.strategy_context import StrategyContext, MarketData, PositionView
from strategies.plugins.futures.active import tmf_spread as mod
from strategies.plugins.futures.active.tmf_spread import (
    TMFSpread, PositionPhase, PositionLifecycle, ReleaseGroup,
    ReleaseGroupStatus, TrailGroup, TrailGroupStatus,
)


@pytest.fixture
def held_paper(monkeypatch, tmp_path):
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("TRADING_FUTURES_MODE", "paper")
    monkeypatch.setattr(mod, "_get_state_file_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(mod, "_MTS_FILL_LOG", str(tmp_path / "fills.jsonl"))
    events = []
    monkeypatch.setattr(mod, "_append_event", lambda name, **kw: events.append((name, kw)))
    monkeypatch.setattr(mod, "_write_mts_state", lambda **kw: None)
    cfg = {"ticker": "TMF", "params": {
        "max_quote_age_ms": 30000, "max_spread_width": 4,
        "release_stop_points": 1000, "trail_distance_points": 1000,
        "release_filter": {"bb_enabled": False},
        "mfe_tighten": {"enabled": False},
    }}
    s = TMFSpread()
    bar = {"ts": datetime.now(), "near_close": 50048.0, "far_close": 50233.0,
           "near_bid": 50047.0, "near_ask": 50049.0,
           "far_bid": 50232.0, "far_ask": 50234.0,
           "near_tick_age_ms": 0.0, "far_tick_age_ms": 0.0,
           "atr": 75.65, "spread_z": 1.0, "volume": 10, "far_volume": 10}
    s.init(StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                           position=PositionView(size=0), config=cfg))
    s._has_position = True
    s._lifecycle = "OPEN"
    s._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE))
    s._near_entry, s._far_entry = 50048.0, 50233.0
    s._near_side, s._far_side = "LONG", "SHORT"
    s._trade_id = "mts-auto-151001-499"
    s._hvwap_execution_mode = "paper_active"
    return s, bar, cfg, events


@pytest.mark.parametrize("age_days", [3, 6])
def test_crossday_inventory_updates_policy_j_through_on_bar(held_paper, age_days):
    s, bar, cfg, events = held_paper
    s._entry_ts = datetime.now() - timedelta(days=age_days)
    s._entry_guard_start_ms = s._entry_ts.timestamp() * 1000
    s._entry_ts_ms = s._entry_guard_start_ms
    s.on_bar(StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                             position=PositionView(size=2), config=cfg))
    assert any(name == "POLICY_J_GUARD_BASELINE" for name, _ in events), events
    assert not any(kw.get("reason") == "ENTRY_TIME_IMPLAUSIBLE" for _, kw in events)
    assert s._pj_durable_peak is not None


def test_new_entry_still_quarantined_through_on_bar(held_paper):
    s, bar, cfg, events = held_paper
    s._entry_ts = datetime.now()
    s._entry_guard_start_ms = s._entry_ts.timestamp() * 1000
    s._entry_ts_ms = s._entry_guard_start_ms
    s.on_bar(StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                             position=PositionView(size=2), config=cfg))
    assert s._pj_guard_state == "QUARANTINE"
    assert s._pj_durable_peak is None
