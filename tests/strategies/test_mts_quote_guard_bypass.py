# 2026-07-16 Gemini CLI: Unit tests for MTS Emergency Quote Guard Bypass.
# Enforces Stop Risk > Quote Accuracy priority hierarchy.
import pytest
import time
from datetime import datetime
from unittest.mock import patch
from strategies.plugins.futures.active.tmf_spread import (
    TMFSpread, PositionPhase, PositionLifecycle, ReleaseGroup,
    ReleaseGroupStatus, TrailGroup, TrailGroupStatus, Leg, Side, LifecycleAction
)
from core.strategy_context import StrategyContext, MarketData, PositionView

def _make_bar(near_close=45500, far_close=45800, atr=10.0, near_age=5000.0, far_age=5000.0):
    return {
        "near_close": near_close,
        "far_close": far_close,
        "atr": atr,
        "near_high": near_close,
        "near_low": near_close,
        "far_high": far_close,
        "far_low": far_close,
        "near_tick_age_ms": near_age,
        "far_tick_age_ms": far_age,
        "spread_z": 0.0,
        "sqz_on": False,
    }

def test_release_stop_bypasses_quote_guard_when_exceeds_threshold(tmp_path):
    """
    If a RELEASE decision is triggered and the loss of that leg exceeds 1.5x the stop threshold,
    the Quote Age Guard is bypassed even if the quote is extremely stale.
    """
    state_file = tmp_path / "test_state.json"
    strategy = TMFSpread()
    
    # Release stop threshold is 20 points, emergency multiplier is 1.5 (threshold = 30 points loss)
    bar_init = _make_bar(near_close=45500, far_close=45800, atr=10.0, near_age=0.0, far_age=0.0)
    
    config = {
        "ticker": "TMF",
        "params": {
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 2.0,
            "release_stop_points": 20,
            "trail_distance_points": 30,
            "confirm_ticks": 1,
            "confirm_ms": 0,
            "max_quote_age_ms": 1000,
            "max_spread_width": 999999,
            "release_filter": {"bb_enabled": False},
            "mfe_tighten": {"enabled": False},
        },
    }
    
    strategy.init(StrategyContext(
        market=MarketData(last_bar=bar_init, ticker="TMF"),
        position=PositionView(size=0),
        config=config,
    ))
    
    # Override path and properties to match an open spread
    strategy._state_file = str(state_file)
    strategy._has_position = True
    strategy._lifecycle = "OPEN"
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    strategy._near_entry = 45500.0
    strategy._far_entry = 45800.0
    strategy._near_side = "LONG"
    strategy._far_side = "SHORT"
    strategy._trade_id = "test-bypass-001"
    strategy._entry_ts = datetime.now()
    strategy._peak = 45500.0
    strategy._nadir = 45800.0
    
    # 1. PnL is within normal range but above stop (-25 pts loss on near leg, which is > 20 but < 30)
    # Stale quote (5000ms > 1000ms limit) should block release.
    bar_stale = _make_bar(near_close=45475, far_close=45800, near_age=5000.0, far_age=5000.0)
    ctx_stale = StrategyContext(market=MarketData(last_bar=bar_stale, ticker="TMF"), position=PositionView(size=2), config=config)
    res_stale = strategy.on_bar(ctx_stale)
    assert res_stale is None
    assert strategy._lifecycle == "OPEN"
    
    # 2. PnL is extremely bad (-50 pts loss on near leg, which is > 30 pts threshold)
    # Stale quote (5000ms) should be bypassed, triggering a RELEASE signal.
    bar_bypass = _make_bar(near_close=45450, far_close=45800, near_age=5000.0, far_age=5000.0)
    ctx_bypass = StrategyContext(market=MarketData(last_bar=bar_bypass, ticker="TMF"), position=PositionView(size=2), config=config)
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        res_bypass = strategy.on_bar(ctx_bypass)
    assert res_bypass is not None
    assert res_bypass.action == "PARTIAL_EXIT"
    assert strategy._lifecycle == "RELEASE_NEAR"


def test_trail_stop_bypasses_quote_guard_when_exceeds_threshold(tmp_path):
    """
    If a TRAIL decision is triggered in SINGLE_LEG phase and giveback exceeds 1.5x trail distance,
    the Quote Age Guard is bypassed.
    """
    state_file = tmp_path / "test_state.json"
    strategy = TMFSpread()
    
    # Trail distance is 20 points, emergency multiplier is 1.5 (threshold = 30 points giveback)
    bar_init = _make_bar(near_close=45500, far_close=45800, atr=10.0, near_age=0.0, far_age=0.0)
    
    config = {
        "ticker": "TMF",
        "params": {
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 2.0,
            "release_stop_points": 20,
            "trail_distance_points": 20,
            "confirm_ticks": 1,
            "confirm_ms": 0,
            "max_quote_age_ms": 1000,
            "max_spread_width": 999999,
            "vwap_exit": {"enabled": False},
            "mfe_tighten": {"enabled": False},
            "post_release": {"breakeven_after_atr": 999, "force_lock_after_atr": 999},
        },
    }
    
    strategy.init(StrategyContext(
        market=MarketData(last_bar=bar_init, ticker="TMF"),
        position=PositionView(size=0),
        config=config,
    ))
    
    # Set single leg remaining NEAR LONG position
    strategy._state_file = str(state_file)
    strategy._has_position = True
    strategy._side = "LONG"
    strategy._released_leg = "far"
    strategy._lifecycle = "TRAILING_LONG"
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.COMPLETED, filled_leg=Leg.FAR),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED, remaining_leg=Leg.NEAR),
    )
    strategy._near_entry = 45500.0
    strategy._near_side = "LONG"
    strategy._trade_id = "test-bypass-002"
    strategy._entry_ts = datetime.now()
    strategy._single_leg_started_at = datetime.now()
    strategy._peak = 45600.0  # Peak is 45600

    # ADR-011 Phase 4: simulate warmup expiry (entered 1s ago, 2 ticks received)
    import time as _time
    strategy._single_leg_entered_mono = _time.monotonic() - 1.0
    strategy._single_leg_post_fill_ticks = 2
    
    # 1. Giveback is -25 pts (price = 45575), which exceeds trail distance (20) but is less than emergency bypass (30)
    # Stale quote should block the exit.
    bar_stale = _make_bar(near_close=45575, far_close=45800, near_age=5000.0, far_age=5000.0)
    ctx_stale = StrategyContext(market=MarketData(last_bar=bar_stale, ticker="TMF"), position=PositionView(size=1), config=config)
    res_stale = strategy.on_bar(ctx_stale)
    assert res_stale is None
    
    # 2. Giveback is -50 pts (price = 45550), which exceeds emergency bypass (30)
    # Stale quote should be bypassed, triggering a full EXIT signal.
    bar_bypass = _make_bar(near_close=45550, far_close=45800, near_age=5000.0, far_age=5000.0)
    ctx_bypass = StrategyContext(market=MarketData(last_bar=bar_bypass, ticker="TMF"), position=PositionView(size=1), config=config)
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        res_bypass = strategy.on_bar(ctx_bypass)
    assert res_bypass is not None
    assert res_bypass.action == "EXIT"


def test_decoupled_leg_freshness(tmp_path):
    """
    Decoupled Leg Freshness: When releasing the NEAR leg, we should only care about
    the near quote's freshness. If NEAR quote is fresh but FAR quote is stale,
    the release stop should NOT be blocked.
    """
    state_file = tmp_path / "test_state.json"
    strategy = TMFSpread()
    
    bar_init = _make_bar(near_close=45500, far_close=45800, atr=10.0, near_age=0.0, far_age=0.0)
    config = {
        "ticker": "TMF",
        "params": {
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 2.0,
            "release_stop_points": 20,
            "trail_distance_points": 30,
            "confirm_ticks": 1,
            "confirm_ms": 0,
            "max_quote_age_ms": 1000,
            "max_spread_width": 999999,
            "release_filter": {"bb_enabled": False},
            "mfe_tighten": {"enabled": False},
        },
    }
    
    strategy.init(StrategyContext(
        market=MarketData(last_bar=bar_init, ticker="TMF"),
        position=PositionView(size=0),
        config=config,
    ))
    
    strategy._state_file = str(state_file)
    strategy._has_position = True
    strategy._lifecycle = "OPEN"
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    strategy._near_entry = 45500.0
    strategy._far_entry = 45800.0
    strategy._near_side = "LONG"
    strategy._far_side = "SHORT"
    strategy._trade_id = "test-decouple-001"
    strategy._entry_ts = datetime.now()
    strategy._peak = 45500.0
    strategy._nadir = 45800.0
    
    # NEAR leg hits stop (loss of 25 pts is > 20 stop).
    # Near quote is fresh (100ms), but Far quote is stale (5000ms).
    # Since we are releasing NEAR leg, Far quote status should be ignored, and release should execute!
    bar_decoupled = _make_bar(near_close=45475, far_close=45800, near_age=100.0, far_age=5000.0)
    ctx_decoupled = StrategyContext(market=MarketData(last_bar=bar_decoupled, ticker="TMF"), position=PositionView(size=2), config=config)
    
    with patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        res = strategy.on_bar(ctx_decoupled)
        
    assert res is not None
    assert res.action == "PARTIAL_EXIT"
    assert strategy._lifecycle == "RELEASE_NEAR"


def test_quote_guard_timeout_force_release(tmp_path):
    """
    Timeout Bypass: If a stop is hit but quote is stale, wait/block initially.
    If the decision remains active and elapsed time exceeds 500ms, force-release anyway.
    """
    state_file = tmp_path / "test_state.json"
    strategy = TMFSpread()
    
    bar_init = _make_bar(near_close=45500, far_close=45800, atr=10.0, near_age=0.0, far_age=0.0)
    config = {
        "ticker": "TMF",
        "params": {
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 2.0,
            "release_stop_points": 20,
            "trail_distance_points": 30,
            "confirm_ticks": 1,
            "confirm_ms": 0,
            "max_quote_age_ms": 1000,
            "max_spread_width": 999999,
            "release_filter": {"bb_enabled": False},
            "mfe_tighten": {"enabled": False},
        },
    }
    
    strategy.init(StrategyContext(
        market=MarketData(last_bar=bar_init, ticker="TMF"),
        position=PositionView(size=0),
        config=config,
    ))
    
    strategy._state_file = str(state_file)
    strategy._has_position = True
    strategy._lifecycle = "OPEN"
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    strategy._near_entry = 45500.0
    strategy._far_entry = 45800.0
    strategy._near_side = "LONG"
    strategy._far_side = "SHORT"
    strategy._trade_id = "test-timeout-001"
    strategy._entry_ts = datetime.now()
    strategy._peak = 45500.0
    strategy._nadir = 45800.0
    
    # 1. First tick: Stop hit (loss is 25 pts), near quote is stale (5000ms).
    # Since this is the first tick, elapsed time is 0. Blocks release.
    bar_tick1 = _make_bar(near_close=45475, far_close=45800, near_age=5000.0, far_age=0.0)
    ctx_tick1 = StrategyContext(market=MarketData(last_bar=bar_tick1, ticker="TMF"), position=PositionView(size=2), config=config)
    
    res1 = strategy.on_bar(ctx_tick1)
    assert res1 is None
    assert strategy._release_pending_mono > 0.0
    
    # 2. Second tick: Quote is still stale. Simulating 600ms elapsed.
    # Should bypass quote guard and force release.
    bar_tick2 = _make_bar(near_close=45475, far_close=45800, near_age=5000.0, far_age=0.0)
    ctx_tick2 = StrategyContext(market=MarketData(last_bar=bar_tick2, ticker="TMF"), position=PositionView(size=2), config=config)
    
    with patch("time.monotonic") as mock_mono, \
         patch("strategies.plugins.futures.active.tmf_spread._append_event"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
        # Set monotonic time to be 600ms (0.6s) after the start time
        mock_mono.return_value = strategy._release_pending_mono + 0.6
        res2 = strategy.on_bar(ctx_tick2)
        
    assert res2 is not None
    assert res2.action == "PARTIAL_EXIT"
    assert strategy._lifecycle == "RELEASE_NEAR"
