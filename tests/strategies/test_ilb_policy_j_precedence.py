"""2026-11-23 Gemini CLI: Regression tests for independent_leg_bracket vs Policy J / HVWAP precedence.

Verifies:
1. When params.independent_leg_bracket.enabled == True:
   - Policy J peak tracking (_update_policy_j_peak) does NOT run and emits no POLICY_J_* events.
   - Policy J COMBINED_EXIT / ATR release+trail / HVWAP paper release NEVER emit RELEASE/EXIT
     even when combined UPL exceeds combined_upl_activation_net_pnl_twd (200 TWD) and gives back
     past combined_upl_giveback_twd (50 TWD), as long as neither leg hits its own +100 / -30 bracket.
   - When a leg DOES hit its own bracket, PARTIAL_EXIT / EXIT carries INDEPENDENT_LEG_BRACKET_* winner.
2. When params.independent_leg_bracket.enabled == False (legacy):
   - Policy J COMBINED_EXIT and ATR RELEASE continue to fire normally.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from core import runtime_paths as rp
from core.strategy_context import MarketData, PositionView, StrategyContext
from strategies.plugins.futures.active import tmf_spread
from strategies.plugins.futures.active.mts_lifecycle_adapter import (
    Leg,
    PositionPhase,
    ReleaseGroupStatus,
    TrailGroupStatus,
)
from strategies.plugins.futures.active.tmf_spread import RecoveryState, TMFSpread


def _build_strategy(tmp_path, monkeypatch, *, ilb_enabled: bool, extra_params: dict | None = None):
    """Initialize a TMFSpread instance in SPREAD phase with isolated runtime paths."""
    monkeypatch.setenv("TRADING_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("TRADING_RUNTIME_ID", raising=False)
    monkeypatch.delenv("MTS_STATE_PATH", raising=False)
    monkeypatch.setenv("MTS_BACKTEST", "1")
    rp.bind_mts_data_mode("paper")

    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        tmf_spread, "_append_event", lambda ev, **kw: events.append((ev, kw))
    )
    monkeypatch.setattr(tmf_spread, "_write_mts_state", lambda **kw: None)
    monkeypatch.setattr(tmf_spread, "_write_mts_telemetry", lambda **kw: None)

    params = {
        "enable_combined_upl_trail": True,
        "combined_upl_activation_net_pnl_twd": 200.0,
        "combined_upl_giveback_twd": 50.0,
        "release_stop_points": 20.0,
        "trail_distance_points": 20.0,
        "confirm_ticks": 1,
        "confirm_ms": 0.0,
        "max_quote_age_ms": 5000.0,
        "max_spread_width": 10.0,
        "entry_peak_guard_ms": 0.0,
        "peak_confirmation_samples": 1,
        "peak_confirmation_ms": 0.0,
        "independent_leg_bracket": {
            "enabled": ilb_enabled,
            "take_profit_pts": 100.0,
            "stop_loss_pts": 30.0,
        },
    }
    if extra_params:
        params.update(extra_params)

    s = TMFSpread()
    ctx = StrategyContext(
        position=PositionView(),
        market=MarketData(ticker="TMF", last_bar={}),
        config={"ticker": "TMF", "params": params},
    )
    s.init(ctx)

    # Put strategy into active SPREAD position: NEAR LONG @ 22000, FAR SHORT @ 22050
    s._has_position = True
    s._mts_recovery_state = RecoveryState.RECOVERED
    s._trade_id = "mts-ilb-precedence-test"
    s._near_entry = 22000.0
    s._far_entry = 22050.0
    s._near_side = "LONG"
    s._far_side = "SHORT"
    s._released_leg = None
    s._side = None
    s._lifecycle = "OPEN"
    s._entry_ts = datetime(2026, 11, 23, 9, 0, 0)
    s._entry_ts_ms = 1000.0
    s._entry_guard_start_ms = 1000.0
    s._lifecycle_oca.phase = PositionPhase.SPREAD
    s._lifecycle_oca.release_group.status = ReleaseGroupStatus.ARMED
    return s, ctx, events


def _make_bar(near_close: float, far_close: float, ts: datetime) -> dict:
    return {
        "timestamp": ts,
        "ts": ts,
        "near_close": near_close,
        "far_close": far_close,
        "near_bid": near_close - 1.0,
        "near_ask": near_close + 1.0,
        "far_bid": far_close - 1.0,
        "far_ask": far_close + 1.0,
        "near_high": near_close,
        "near_low": near_close,
        "far_high": far_close,
        "far_low": far_close,
        "near_tick_age_ms": 10.0,
        "far_tick_age_ms": 10.0,
        "quote_age_ms": 10.0,
        "pair_skew_ms": 5.0,
        "spread_z": 0.5,
        "atr": 15.0,
        "trade_id": "mts-ilb-precedence-test",
    }


def _call_on_bar(strategy: TMFSpread, ctx: StrategyContext, near_close: float, far_close: float, ts: datetime):
    bar_ctx = StrategyContext(
        position=ctx.position,
        market=MarketData(ticker="TMF", last_bar=_make_bar(near_close, far_close, ts)),
        config=ctx.config,
    )
    return strategy.on_bar(bar_ctx)


def test_ilb_enabled_blocks_policy_j_and_hvwap_and_atr_release(tmp_path, monkeypatch):
    """When independent_leg_bracket.enabled=True, Policy J activation+giveback and ATR stop
    must NOT emit any RELEASE/EXIT signal or POLICY_J_* events while legs are inside +100/-30."""
    s, ctx, events = _build_strategy(tmp_path, monkeypatch, ilb_enabled=True)

    # Seed a pre-existing high Policy J peak AND a hostile HVWAP hook to verify both are bypassed.
    s._peak_net_exit_pnl_twd = 450.0
    hvwap_called = False

    def _spy_hvwap(*args, **kwargs):
        nonlocal hvwap_called
        hvwap_called = True
        return SimpleNamespace(action="PARTIAL_EXIT", reason="TMF_RELEASE_NEAR")

    monkeypatch.setattr(s, "_hvwap_maybe_paper_release", _spy_hvwap)

    t1 = datetime(2026, 11, 23, 9, 5, 0)
    # Near +45 pts, Far +10 pts -> combined net PnL = +458 TWD (>= 200 TWD Policy J target),
    # neither leg hits +100 / -30.
    sig1 = _call_on_bar(s, ctx, 22045.0, 22040.0, t1)
    assert sig1 is None

    t2 = t1 + timedelta(seconds=5)
    # Giveback: Near +25 pts, Far +5 pts -> combined net PnL = +208 TWD (250 TWD giveback >> 50 TWD),
    # still neither leg hits +100 / -30.
    sig2 = _call_on_bar(s, ctx, 22025.0, 22045.0, t2)
    assert sig2 is None

    t3 = t2 + timedelta(seconds=5)
    # Near -25 pts (breaches legacy ATR release_stop=20, but inside ILB stop_loss=30), Far +5 pts.
    sig3 = _call_on_bar(s, ctx, 21975.0, 22045.0, t3)
    assert sig3 is None

    assert not hvwap_called, "HVWAP paper release must never run when independent_leg_bracket is enabled"
    assert getattr(s, "_pj_durable_peak", None) is None, "Policy J peak tracker must not run when independent_leg_bracket is enabled"
    pj_events = [ev for ev, _ in events if ev.startswith("POLICY_J_")]
    assert pj_events == [], f"Unexpected Policy J events under ILB: {pj_events}"


def test_ilb_enabled_triggers_own_bracket_with_winner_tag(tmp_path, monkeypatch):
    """When a leg crosses its own +100/-30 bracket under ILB, it emits PARTIAL_EXIT / EXIT
    with INDEPENDENT_LEG_BRACKET_* winner."""
    s, ctx, _ = _build_strategy(tmp_path, monkeypatch, ilb_enabled=True)

    t1 = datetime(2026, 11, 23, 9, 6, 0)
    # Near hits -30 pts stop-loss while far is flat -> RELEASE NEAR
    sig_rel = _call_on_bar(s, ctx, 21970.0, 22050.0, t1)
    assert sig_rel is not None
    assert sig_rel.action == "PARTIAL_EXIT"
    assert sig_rel.reason == "TMF_RELEASE_NEAR"
    assert sig_rel.winner == "INDEPENDENT_LEG_BRACKET_STOP_LOSS"

    # Transition to SINGLE_LEG (near filled, far SHORT remaining @ 22050)
    s._released_leg = "near"
    s._side = "SHORT"
    s._lifecycle = "TRAILING_SHORT"
    s._single_leg_last_tick_ts = 0.0
    s._single_leg_entered_mono = 0.0
    s._lifecycle_oca.phase = PositionPhase.SINGLE_LEG
    s._lifecycle_oca.release_group.status = ReleaseGroupStatus.COMPLETED
    s._lifecycle_oca.release_group.filled_leg = Leg.NEAR
    s._lifecycle_oca.trail_group.status = TrailGroupStatus.ARMED
    s._lifecycle_oca.trail_group.remaining_leg = Leg.FAR

    t2 = t1 + timedelta(seconds=10)
    # Far SHORT @ 22050 -> 21950 = +100 pts take-profit -> EXIT with ILB TP winner
    sig_exit = _call_on_bar(s, ctx, 21970.0, 21950.0, t2)
    assert sig_exit is not None
    assert sig_exit.action == "EXIT"
    assert sig_exit.reason == "TMF_INDEPENDENT_LEG_BRACKET_TAKE_PROFIT"
    assert sig_exit.winner == "INDEPENDENT_LEG_BRACKET_TAKE_PROFIT"


def test_ilb_disabled_legacy_policy_j_and_atr_release_still_work(tmp_path, monkeypatch):
    """Regression: when independent_leg_bracket.enabled=False, Policy J COMBINED_EXIT
    and legacy ATR RELEASE continue to fire as before."""
    # Case A: Policy J COMBINED_EXIT fires on activation + giveback
    s_pj, ctx_pj, _ = _build_strategy(tmp_path, monkeypatch, ilb_enabled=False)

    t1 = datetime(2026, 11, 23, 9, 10, 0)
    # Tick 1: Near +45 pts, Far +10 pts -> combined net PnL = +458 TWD (>= 200 TWD) establishes durable peak
    sig_pj1 = _call_on_bar(s_pj, ctx_pj, 22045.0, 22040.0, t1)
    assert sig_pj1 is None

    t2 = t1 + timedelta(seconds=5)
    # Tick 2: Combined PnL = (25 + 5) * 10 - 92 = 208 TWD; peak=458 -> giveback=250 >= 50 -> COMBINED_EXIT
    sig_pj2 = _call_on_bar(s_pj, ctx_pj, 22025.0, 22045.0, t2)
    assert sig_pj2 is not None
    assert sig_pj2.action == "EXIT"
    assert sig_pj2.reason == "TMF_COMBINED_EXIT"

    # Case B: Legacy ATR RELEASE fires when Policy J is disabled and near breaches ATR stop (15 * 2.5 = 37.5)
    s_atr, ctx_atr, _ = _build_strategy(
        tmp_path, monkeypatch, ilb_enabled=False,
        extra_params={"enable_combined_upl_trail": False},
    )
    sig_atr = _call_on_bar(s_atr, ctx_atr, 21950.0, 22050.0, t1)
    assert sig_atr is not None
    assert sig_atr.action == "PARTIAL_EXIT"
    assert sig_atr.reason == "TMF_RELEASE_NEAR"
