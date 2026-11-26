"""2026-11-26 Gemini CLI: Contract tests for EXIT_LOG trigger_pnl / trigger_price / trigger_basis.

Verifies that when INDEPENDENT_LEG_BRACKET triggers on exit-side BBO (ask for SHORT,
bid for LONG), EXIT_LOG exposes the actual decision trigger_pnl / trigger_price /
trigger_basis while preserving existing pnl and pnl_basis semantics.
"""
from datetime import datetime
from unittest.mock import patch

from core.strategy_context import MarketData, PositionView, StrategyContext
from strategies.plugins.futures.active.tmf_spread import (
    Leg,
    PositionLifecycle,
    PositionPhase,
    ReleaseGroup,
    ReleaseGroupStatus,
    TMFSpread,
    TrailGroup,
    TrailGroupStatus,
)


def _make_bar(near_close, far_close, **kw):
    bar = {
        "near_close": near_close, "far_close": far_close, "atr": 180.0,
        "near_bid": near_close - 2.0, "near_ask": near_close,
        "far_bid": far_close, "far_ask": far_close + 10.0,
        "bbo_near_bid": near_close - 2.0, "bbo_near_ask": near_close,
        "bbo_far_bid": far_close, "bbo_far_ask": far_close + 10.0,
        "bbo_near_age_ms": 10, "bbo_far_age_ms": 20,
        "near_high": near_close + 20, "near_low": near_close - 20,
        "far_high": far_close + 20, "far_low": far_close - 20,
        "near_tick_age_ms": 0.0, "far_tick_age_ms": 0.0,
        "spread_z": 0.0, "timestamp": datetime.now(),
        "near_vwap": near_close, "far_vwap": far_close,
        "sqz_on": False,
    }
    bar.update(kw)
    if "bbo_near_bid" in kw and "near_bid" not in kw:
        bar["near_bid"] = kw["bbo_near_bid"]
    if "bbo_near_ask" in kw and "near_ask" not in kw:
        bar["near_ask"] = kw["bbo_near_ask"]
    if "bbo_far_bid" in kw and "far_bid" not in kw:
        bar["far_bid"] = kw["bbo_far_bid"]
    if "bbo_far_ask" in kw and "far_ask" not in kw:
        bar["far_ask"] = kw["bbo_far_ask"]
    return bar


def _make_armed_strategy(tmp_path, *, phase=PositionPhase.SPREAD, released_leg=None):
    s = TMFSpread()
    config = {"ticker": "TMF", "params": {
        "atr_multiplier_stop": 2.5, "atr_multiplier_trail": 1.0,
        "release_stop_points": 88, "trail_distance_points": 66,
        "confirm_ticks": 1, "confirm_ms": 0, "max_quote_age_ms": 999999,
        "max_spread_width": 999999, "mfe_tighten": {"enabled": False},
        "post_release": {"breakeven_after_atr": 999, "force_lock_after_atr": 999},
        "independent_leg_bracket": {"enabled": True, "take_profit_pts": 100.0, "stop_loss_pts": 30.0},
    }}
    init_bar = _make_bar(39208.0, 39304.0)
    s.init(StrategyContext(market=MarketData(last_bar=init_bar, ticker="TMF"),
                           position=PositionView(size=0), config=config))
    s._state_file = str(tmp_path / "state.json")
    s._has_position = True
    s._near_entry = 39208.0
    s._far_entry = 39304.0
    s._near_side = "SHORT"
    s._far_side = "LONG"
    s._side = "LONG"
    s._trade_id = "mts-auto-20261126-151833-024"
    s._entry_ts = datetime.now()
    s._peak, s._nadir = 39304.0, 39208.0
    s._release_near_ticks = s._release_far_ticks = 0
    s._release_near_start_time = s._release_far_start_time = 0.0
    s._release_mono = 0.0
    if phase == PositionPhase.SPREAD:
        s._lifecycle = "OPEN"
        s._released_leg = None
        s._lifecycle_oca = PositionLifecycle(
            phase=PositionPhase.SPREAD,
            release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
            trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
        )
    else:
        s._lifecycle = "SINGLE_LEG"
        s._released_leg = released_leg or "near"
        s._lifecycle_oca = PositionLifecycle(
            phase=PositionPhase.SINGLE_LEG,
            release_group=ReleaseGroup(status=ReleaseGroupStatus.FILLED, filled_leg=Leg.NEAR),
            trail_group=TrailGroup(status=TrailGroupStatus.ARMED, remaining_leg=Leg.FAR),
        )
    return s, config


def test_t1_exit_log_exposes_bbo_trigger_pnl_when_close_differs_from_ask(tmp_path):
    """T1: near_close=39233 (-25) vs bbo_near_ask=39238 (-30) -> EXIT_LOG reports trigger_pnl=-30 while preserving pnl=-25."""
    s, config = _make_armed_strategy(tmp_path, phase=PositionPhase.SPREAD)
    bar = _make_bar(
        near_close=39233.0, far_close=39334.0,
        bbo_near_bid=39233.0, bbo_near_ask=39238.0,
        bbo_far_bid=39330.0, bbo_far_ask=39340.0,
    )
    events = []
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event",
               side_effect=lambda ev, **kw: events.append((ev, kw))):
        ctx = StrategyContext(market=MarketData(last_bar=bar, ticker="TMF"),
                              position=PositionView(size=2), config=config)
        s.on_bar(ctx)

    exit_logs = [kw for ev, kw in events if ev == "EXIT_LOG"]
    assert len(exit_logs) == 1
    log = exit_logs[0]
    assert log["exit_reason"] == "INDEPENDENT_LEG_BRACKET_STOP_LOSS"
    assert log["pnl"] == -25.0
    assert log["pnl_basis"] == "TRIGGER_PTS"
    assert log["trigger_pnl"] == -30.0
    assert log["trigger_price"] == 39238.0
    assert log["trigger_basis"] == "BBO_ASK"


def test_t2_exit_log_trigger_pnl_matches_pnl_when_close_equals_bbo(tmp_path):
    """T2: When close == exit-side BBO (both spread SL and single-leg TP), trigger_pnl == pnl with zero divergence."""
    # Case A: SPREAD NEAR SHORT SL with near_close == bbo_near_ask == 39238.0
    s1, config1 = _make_armed_strategy(tmp_path / "a", phase=PositionPhase.SPREAD)
    bar1 = _make_bar(
        near_close=39238.0, far_close=39334.0,
        bbo_near_bid=39235.0, bbo_near_ask=39238.0,
        bbo_far_bid=39330.0, bbo_far_ask=39340.0,
    )
    events1 = []
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event",
               side_effect=lambda ev, **kw: events1.append((ev, kw))):
        s1.on_bar(StrategyContext(market=MarketData(last_bar=bar1, ticker="TMF"),
                                  position=PositionView(size=2), config=config1))

    logs1 = [kw for ev, kw in events1 if ev == "EXIT_LOG"]
    assert len(logs1) == 1
    assert logs1[0]["pnl"] == -30.0
    assert logs1[0]["trigger_pnl"] == -30.0
    assert logs1[0]["trigger_price"] == 39238.0
    assert logs1[0]["trigger_basis"] == "BBO_ASK"

    # Case B: SINGLE_LEG FAR LONG TP with far_close == bbo_far_bid == 39407.0
    s2, config2 = _make_armed_strategy(tmp_path / "b", phase=PositionPhase.SINGLE_LEG, released_leg="near")
    bar2 = _make_bar(
        near_close=39317.0, far_close=39407.0,
        bbo_near_bid=39315.0, bbo_near_ask=39318.0,
        bbo_far_bid=39407.0, bbo_far_ask=39420.0,
    )
    events2 = []
    with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_fill"), \
         patch("strategies.plugins.futures.active.tmf_spread._append_event",
               side_effect=lambda ev, **kw: events2.append((ev, kw))):
        s2.on_bar(StrategyContext(market=MarketData(last_bar=bar2, ticker="TMF"),
                                  position=PositionView(size=1), config=config2))

    logs2 = [kw for ev, kw in events2 if ev == "EXIT_LOG"]
    assert len(logs2) == 1
    assert logs2[0]["exit_reason"] == "INDEPENDENT_LEG_BRACKET_TAKE_PROFIT"
    assert logs2[0]["pnl"] == 103.0
    assert logs2[0]["trigger_pnl"] == 103.0
    assert logs2[0]["trigger_price"] == 39407.0
    assert logs2[0]["trigger_basis"] == "BBO_BID"
