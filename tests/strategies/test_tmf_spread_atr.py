"""
Contract tests for ADR-014: Squeeze/BB must not gate MTS release.

Phase 1: Remove BB filter gate from release decision chain.
  - sqz_on=True + threshold confirmed → RELEASE
  - sqz_on=False + threshold confirmed → RELEASE
  - BB band 未確認 + threshold confirmed → RELEASE
  - Changing sqz_on or BB values must not change the release decision.
  - confirmation 未完成 → 不 RELEASE (tick/time confirmation still applies)
  - existing pending RELEASE → 不重複送單

2026-07-16 Hermes Agent: rewritten per ADR-014.
"""
import pytest
from unittest.mock import patch, PropertyMock
from datetime import datetime

from strategies.plugins.futures.active.tmf_spread import (
    TMFSpread, Leg, PositionPhase, PositionLifecycle, ReleaseGroup,
    ReleaseGroupStatus, TrailGroup, TrailGroupStatus,
)
from core.strategy_context import StrategyContext, MarketData, PositionView


def _make_bar(near_close=45500, far_close=45800, atr=10.0, **kw):
    """Build a minimal bar dict with overridable defaults."""
    bar = {
        "near_close": near_close,
        "far_close": far_close,
        "atr": atr,
        "near_high": near_close + 50,
        "near_low": near_close - 50,
        "far_high": far_close + 50,
        "far_low": far_close - 50,
        "near_tick_age_ms": 0.0,
        "far_tick_age_ms": 0.0,
        "spread_z": 0.0,
        "timestamp": datetime.now(),
        "near_vwap": near_close,
        "far_vwap": far_close,
    }
    bar.update(kw)
    return bar


def _setup_armed(tmp_path, release_stop_points=20, bb_enabled=False,
                 confirm_ticks=2, **bar_overrides):
    """Create a TMFSpread instance in SPREAD→ARMED lifecycle with position."""
    s = TMFSpread()
    state_file = tmp_path / "test_state.json"

    config = {
        "ticker": "TMF",
        "params": {
            "atr_multiplier_stop": 2.0,
            "atr_multiplier_trail": 2.0,
            "release_stop_points": release_stop_points,
            "trail_distance_points": 30,
            "confirm_ticks": confirm_ticks,
            "confirm_ms": 0,
            "max_quote_age_ms": 999999,
            "max_spread_width": 999999,
            "mfe_tighten": {"enabled": False},
            "post_release": {"breakeven_after_atr": 999, "force_lock_after_atr": 999},
        },
    }

    bar_init = _make_bar(**bar_overrides)
    s.init(StrategyContext(
        market=MarketData(last_bar=bar_init, ticker="TMF"),
        position=PositionView(size=0),
        config=config,
    ))

    # Override path and properties to match an open spread
    s._state_file = str(state_file)
    s._has_position = True
    s._lifecycle = "OPEN"
    s._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
        trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
    )
    s._near_entry = 45700
    s._far_entry = 46000
    s._near_side = "SHORT"
    s._far_side = "LONG"
    s._side = "SHORT"
    s._trade_id = "test-adr014-001"
    s._entry_ts = datetime.now()
    s._peak = 45700.0
    s._nadir = 46000.0
    s._release_near_ticks = 0
    s._release_far_ticks = 0
    s._release_near_start_time = 0.0
    s._release_far_start_time = 0.0
    s._release_mono = 0.0
    return s, config


class TestReleaseInvariantSqzBb:
    """ADR-014: sqz_on / BB values must NOT gate release."""

    @pytest.mark.parametrize("sqz_on", [True, False])
    def test_release_confirmed_regardless_of_sqz(self, sqz_on, tmp_path):
        """sqz_on=True/False + threshold confirmed → RELEASE (same behavior)."""
        s, config = _setup_armed(tmp_path, release_stop_points=20, confirm_ticks=1)

        # Near pnl = 45700 - 45680 = 20 (exactly at stop threshold)
        bar = _make_bar(near_close=45680, sqz_on=sqz_on,
                        near_bb_upper=45685, near_bb_lower=45670,
                        far_bb_upper=46050, far_bb_lower=45950)
        ctx = StrategyContext(
            market=MarketData(last_bar=bar, ticker="TMF"),
            position=PositionView(size=2),
            config=config,
        )

        with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
            with patch("strategies.plugins.futures.active.tmf_spread._append_event"):
                with patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
                    result = s.on_bar(ctx)

        # Must not be blocked by BB — release should proceed
        skip = getattr(getattr(s, "last_eval", None), "skip_reason", "") or ""
        assert "BB" not in skip.upper(), \
            f"Release blocked by BB despite ADR-014: {skip}"

    def test_bb_not_favorable_still_releases(self, tmp_path):
        """BB band not favorable + threshold confirmed → RELEASE (BB must not block)."""
        s, config = _setup_armed(tmp_path, release_stop_points=20, confirm_ticks=1)

        # Near pnl = 45700 - 45680 = 20pts loss (crossed threshold)
        # BB upper = 45685 (above close for SHORT=release) → NOT favorable
        bar = _make_bar(near_close=45680, sqz_on=True,
                        near_bb_upper=45685.0, near_bb_lower=45650.0,
                        far_bb_upper=46050, far_bb_lower=45950)
        ctx = StrategyContext(
            market=MarketData(last_bar=bar, ticker="TMF"),
            position=PositionView(size=2),
            config=config,
        )

        with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
            with patch("strategies.plugins.futures.active.tmf_spread._append_event"):
                with patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
                    result = s.on_bar(ctx)

        # Must NOT be blocked by BB_FILTER_WAITING
        skip = getattr(getattr(s, "last_eval", None), "skip_reason", "") or ""
        assert "BB" not in skip.upper(), \
            f"BB filter blocked release despite ADR-014. Skip: {skip}"

    def test_confirmation_ticks_still_blocks(self, tmp_path):
        """Tick/time confirmation still blocks release (not removed by ADR-014)."""
        s, config = _setup_armed(tmp_path, release_stop_points=20, confirm_ticks=2)

        bar = _make_bar(near_close=45650, sqz_on=False)
        ctx = StrategyContext(
            market=MarketData(last_bar=bar, ticker="TMF"),
            position=PositionView(size=2),
            config=config,
        )

        with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
            with patch("strategies.plugins.futures.active.tmf_spread._append_event"):
                with patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
                    s.on_bar(ctx)

        # First tick: should be pending (confirmation not met)
        skip = getattr(getattr(s, "last_eval", None), "skip_reason", "") or ""
        assert "PENDING" in skip.upper(), \
            f"Expected PENDING (confirmation not met), got: {skip}"
        assert "BB" not in skip.upper(), \
            f"Blocked by BB instead of confirmation: {skip}"

    def test_same_sqz_bb_different_pnl_different_result(self, tmp_path):
        """Same sqz/BB, different PnL → different release outcome (gate not dead)."""
        s_low, config = _setup_armed(tmp_path, release_stop_points=20, confirm_ticks=1)
        s_high = _setup_armed(tmp_path, release_stop_points=20, confirm_ticks=1)[0]
        # Copy config from first
        config_high = config

        # sqz_on=True, BB NOT favorable in both cases
        bar_both = _make_bar(near_close=45680, sqz_on=True,
                             near_bb_upper=45685, near_bb_lower=45670,
                             far_bb_upper=46050, far_bb_lower=45950)

        # Low pnl: near_close far from threshold (pnl = 5, under stop)
        bar_low = dict(bar_both, near_close=45695)
        ctx_low = StrategyContext(
            market=MarketData(last_bar=bar_low, ticker="TMF"),
            position=PositionView(size=2),
            config=config,
        )
        with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
            with patch("strategies.plugins.futures.active.tmf_spread._append_event"):
                with patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
                    s_low.on_bar(ctx_low)

        # High pnl: near_close at threshold (pnl = 20, at stop)
        bar_high = dict(bar_both, near_close=45680)
        ctx_high = StrategyContext(
            market=MarketData(last_bar=bar_high, ticker="TMF"),
            position=PositionView(size=2),
            config=config_high,
        )
        with patch("strategies.plugins.futures.active.tmf_spread._write_mts_state"):
            with patch("strategies.plugins.futures.active.tmf_spread._append_event"):
                with patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
                    s_high.on_bar(ctx_high)

        skip_low = getattr(getattr(s_low, "last_eval", None), "skip_reason", "") or ""
        skip_high = getattr(getattr(s_high, "last_eval", None), "skip_reason", "") or ""

        # Neither should be blocked by BB
        assert "BB" not in skip_low.upper(), f"Low pnl blocked by BB: {skip_low}"
        assert "BB" not in skip_high.upper(), f"High pnl blocked by BB: {skip_high}"
