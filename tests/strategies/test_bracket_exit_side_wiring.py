"""2026-11-25 Hermes Agent: WIRING test — the bracket must actually be called
with exit-side prices.

The helper-level tests passed even after reverting the callsite to `close`
(verified by mutation), i.e. they did not protect the invariant. This test
spies on `_bracket_exit_price` while driving the real management path, so
reverting the callsite makes it fail.
"""
from unittest.mock import patch
from datetime import datetime

import pytest

from strategies.plugins.futures.active.tmf_spread import (
    TMFSpread, Leg, PositionPhase, PositionLifecycle, ReleaseGroup,
    ReleaseGroupStatus, TrailGroup, TrailGroupStatus,
)
from core.strategy_context import StrategyContext, MarketData, PositionView


def _make_bar(near_close=37413, far_close=37537, **kw):
    bar = {
        "near_close": near_close, "far_close": far_close, "atr": 20.0,
        "bbo_near_bid": 37410, "bbo_near_ask": 37416,
        "bbo_far_bid": 37537, "bbo_far_ask": 37558,
        "bbo_near_age_ms": 10, "bbo_far_age_ms": 20,
        "near_high": near_close + 50, "near_low": near_close - 50,
        "far_high": far_close + 50, "far_low": far_close - 50,
        "near_tick_age_ms": 0.0, "far_tick_age_ms": 0.0,
        "spread_z": 0.0, "timestamp": datetime.now(),
        "near_vwap": near_close, "far_vwap": far_close,
        "sqz_on": False,
    }
    bar.update(kw)
    return bar


@pytest.fixture
def armed(tmp_path):
    s = TMFSpread()
    config = {"ticker": "TMF", "params": {
        "atr_multiplier_stop": 2.0, "atr_multiplier_trail": 2.0,
        "release_stop_points": 88, "trail_distance_points": 30,
        "confirm_ticks": 1, "confirm_ms": 0, "max_quote_age_ms": 999999,
        "max_spread_width": 999999, "mfe_tighten": {"enabled": False},
        "post_release": {"breakeven_after_atr": 999, "force_lock_after_atr": 999},
        "independent_leg_bracket": {"enabled": True, "take_profit_pts": 100.0,
                                    "stop_loss_pts": 30.0},
    }}
    bar = _make_bar()
    s.init(StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                           position=PositionView(size=0), config=config))
    s._state_file = str(tmp_path / "state.json")
    s._has_position = True
    s._lifecycle = "OPEN"
    s._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    s._near_entry = 37424.0
    s._far_entry = 37525.0
    s._near_side = "LONG"
    s._far_side = "SHORT"
    s._side = "LONG"
    s._trade_id = "test-bracket-wiring"
    s._entry_ts = datetime.now()
    s._peak, s._nadir = 37424.0, 37525.0
    s._release_near_ticks = s._release_far_ticks = 0
    s._release_near_start_time = s._release_far_start_time = 0.0
    s._release_mono = 0.0
    return s, config


def test_bracket_called_with_exit_side_prices(armed):
    """Spy proves the ILB path asks for exit-side prices (not `close`)."""
    s, config = armed
    calls = []

    def _spy(_self, leg, bar, fallback_close):
        calls.append((leg, fallback_close))
        return {"near": 37416.0, "far": 37558.0}[leg]     # ask for SHORT far

    with patch.object(TMFSpread, "_bracket_exit_price", _spy), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
        ctx = StrategyContext(market=MarketData(last_bar=_make_bar(), ticker="TMF"),
                              position=PositionView(size=2), config=config)
        s.on_bar(ctx)

    assert calls, "bracket was never evaluated with exit-side prices (wiring reverted?)"
    legs = {c[0] for c in calls}
    assert legs == {"near", "far"}
