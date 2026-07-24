"""
Unit tests for cumulative realized PnL tracking.

2026-07-08 Hermes Agent:
Verifies that cumulative_realized_pnl correctly accumulates across
PARTIAL_RELEASE → PM2 restart → SINGLE_LEG trail close lifecycle.
"""
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from strategies.plugins.futures.active.tmf_spread import (
    TMFSpread,
    _write_mts_state,
    lifecycle_to_dict,
    PositionLifecycle,
    PositionPhase,
    ReleaseGroupStatus,
    ReleaseGroup,
    TrailGroup,
    TrailGroupStatus,
    Leg,
)
from core.strategy_context import StrategyContext, MarketData, PositionView


@pytest.fixture
def tmp_state_file(tmp_path, monkeypatch):
    """Redirect MTS state file to a temp path."""
    state_path = tmp_path / "mts_position_state.json"
    monkeypatch.setattr(
        "strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE",
        str(state_path),
    )
    return state_path


def _make_bar(near_close=45500, far_close=45800, atr=80.0):
    return {
        "near_close": near_close,
        "far_close": far_close,
        "atr": atr,
        "near_high": near_close + 50,
        "near_low": near_close - 50,
        "far_high": far_close + 50,
        "far_low": far_close - 50,
        "spread_z": 0.0,
        "sqz_on": False,
    }


class TestCumulativeRealizedPnL:
    """End-to-end: entry → release → restart → trail close → cumulative."""

    def test_full_lifecycle_cumulative(self, tmp_state_file):
        """
        PARTIAL_RELEASE → PM2 restart → SINGLE_LEG trail close.
        cumulative = released_leg_realized + trail_exit_realized.
        """
        strategy = TMFSpread()

        # ── Phase 1: ENTRY ──
        bar = _make_bar(near_close=45920, far_close=46076, atr=80.0)
        with patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", str(tmp_state_file)):
            strategy.init(StrategyContext(
                market=MarketData(last_bar=bar, ticker="TMF"),
                position=PositionView(size=0),
                config={
                    "ticker": "TMF",
                    "params": {
                        "atr_multiplier_stop": 2.0,
                        "atr_multiplier_trail": 2.0,
                        "release_stop_points": 20,
                        "trail_distance_points": 30,
                        "confirm_ticks": 1,  # speed up: no tick confirmation wait
                        "confirm_ms": 0,
                        "max_quote_age_ms": 999999,  # disable quote age gate
                        "max_spread_width": 999999,  # disable spread width gate
                    },
                },
            ))
            strategy._has_position = True
            strategy._lifecycle = "OPEN"
            strategy._lifecycle_oca = PositionLifecycle(
                phase=PositionPhase.SPREAD,
                release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
                trail_group=TrailGroup(status=TrailGroupStatus.INACTIVE),
            )
            strategy._near_entry = 45920.0
            strategy._far_entry = 46076.0
            strategy._near_side = "SHORT"
            strategy._far_side = "LONG"
            strategy._trade_id = "test-cumulative-001"
            strategy._entry_ts = __import__("datetime").datetime.now()
            strategy._peak = 45920.0
            strategy._nadir = 46076.0
            strategy._released_leg = None
            strategy._release_price = 0.0
            strategy._last_atr = 80.0
            strategy._mfe_pts = 0.0
            strategy._mae_pts = 0.0
            strategy._near_max = 45920.0
            strategy._near_min = 45920.0
            strategy._far_max = 46076.0
            strategy._far_min = 46076.0

            # Write initial state (both legs held)
            _write_mts_state(
                has_position=True, action="OPEN", reason="test_entry",
                near_entry=45920.0, far_entry=46076.0,
                near_last=45920.0, far_last=46076.0,
                near_side="SHORT", far_side="LONG",
                near_status="OPEN", far_status="OPEN",
                spread_z=0.0,
                released_leg=None, release_price=0.0,
                trail_peak=45920.0, trail_nadir=46076.0,
                trade_id="test-cumulative-001", ticker="TMF",
                atr=80.0,
                lifecycle=lifecycle_to_dict(strategy._lifecycle_oca),
            )

        # Verify initial state
        state = json.loads(tmp_state_file.read_text())
        assert state["has_position"] is True
        assert state["cumulative_realized_pnl"] == 0.0
        assert state["total_realized_pnl"] == 0.0

        # ── Phase 2: RELEASE NEAR (partial) ──
        # Near SHORT, released at 44800 → realized = (45920-44800)*10 = 11200
        near_upl_pts = 45920.0 - 44800.0  # SHORT: entry - current
        near_realized = near_upl_pts * 10  # TMF multiplier

        with patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", str(tmp_state_file)):
            strategy._released_leg = "near"
            strategy._release_price = 44800.0
            strategy._near_entry = 45920.0  # ensure not lost
            strategy._far_entry = 46076.0
            strategy._lifecycle = "RELEASE_NEAR"
            strategy._lifecycle_oca.phase = PositionPhase.SINGLE_LEG
            strategy._lifecycle_oca.release_group.status = ReleaseGroupStatus.FILLED
            strategy._lifecycle_oca.release_group.filled_leg = Leg.NEAR
            strategy._lifecycle_oca.trail_group.status = TrailGroupStatus.ARMED
            strategy._lifecycle_oca.trail_group.remaining_leg = Leg.FAR

            # Write state with near released (this is the RELEASE signal path)
            _write_mts_state(
                has_position=True, action="RELEASE_NEAR",
                reason="near_pnl_release",
                near_entry=45920.0, far_entry=46076.0,
                near_last=44800.0, far_last=46076.0,
                near_side="SHORT", far_side="LONG",
                near_status="RELEASED", far_status="OPEN",
                spread_z=0.0,
                released_leg="near", release_price=44800.0,
                trail_peak=45920.0, trail_nadir=46076.0,
                trade_id="test-cumulative-001", ticker="TMF",
                atr=80.0,
                lifecycle=lifecycle_to_dict(strategy._lifecycle_oca),
            )

        # Verify released leg realized is in state
        state = json.loads(tmp_state_file.read_text())
        assert state["has_position"] is True
        assert state["near_status"] == "RELEASED"
        assert state["near_realized_pnl"] == pytest.approx(near_realized, rel=0.1)
        assert state["far_realized_pnl"] == 0.0
        assert state["cumulative_realized_pnl"] == 0.0  # NOT accumulated yet!

        released_near_realized = state["near_realized_pnl"]

        # ── Phase 3: PM2 RESTART simulation ──
        # Reset strategy in-memory state, then restore from state file
        strategy2 = TMFSpread()
        bar2 = _make_bar(near_close=44800, far_close=46080, atr=80.0)

        with patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", str(tmp_state_file)):
            strategy2.init(StrategyContext(
                market=MarketData(last_bar=bar2, ticker="TMF"),
                position=PositionView(size=0),
                config={
                    "ticker": "TMF",
                    "params": {
                        "atr_multiplier_stop": 2.0,
                        "atr_multiplier_trail": 2.0,
                        "release_stop_points": 20,
                        "trail_distance_points": 30,
                        "confirm_ticks": 1,
                        "confirm_ms": 0,
                        "max_quote_age_ms": 999999,
                        "max_spread_width": 999999,
                    },
                },
            ))

            # Simulate what on_bar does: restore position from state file
            restored = strategy2._restore_position_state()
            assert restored is True, "restore should succeed"
            assert strategy2._has_position is True
            assert strategy2._near_entry == 45920.0
            assert strategy2._far_entry == 46076.0
            assert strategy2._near_side == "SHORT"
            assert strategy2._far_side == "LONG"
            assert strategy2._released_leg == "near"
            assert strategy2._lifecycle_oca.phase == PositionPhase.SINGLE_LEG

        # ── Phase 4: TRAIL EXIT FAR ──
        # Far LONG, trail exit at 46120 → realized = (46120-46076)*10 - cost
        far_exit_price = 46120.0
        far_pnl_pts = far_exit_price - 46076.0  # LONG: current - entry
        _mult = 10.0
        _turnover = (46076.0 + far_exit_price) * _mult
        _cost = 40.0 + _turnover * 2e-5
        trail_exit_realized = far_pnl_pts * _mult - _cost  # ≈ 440 - 40 - small = ~381.6

        with patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", str(tmp_state_file)):
            # Mock _append_fill to avoid side effects
            with patch("strategies.plugins.futures.active.tmf_spread._append_fill"):
                strategy2._reset(
                    reason="trail_exit_confirmed",
                    exit_price=far_exit_price,
                )

        # ── VERIFY ──
        state = json.loads(tmp_state_file.read_text())
        assert state["has_position"] is False, "position should be FLAT after close"
        assert state["state"] == "CLOSE"

        expected_cumulative = released_near_realized + trail_exit_realized
        actual_cumulative = state["cumulative_realized_pnl"]

        print(f"\nReleased near realized: {released_near_realized:.1f} TWD")
        print(f"Trail exit far realized: {trail_exit_realized:.1f} TWD")
        print(f"Expected cumulative:     {expected_cumulative:.1f} TWD")
        print(f"Actual cumulative:       {actual_cumulative:.1f} TWD")

        assert actual_cumulative == pytest.approx(expected_cumulative, rel=0.01), (
            f"cumulative should be released_near + trail_exit, "
            f"got {actual_cumulative:.1f}, expected ~{expected_cumulative:.1f}"
        )
        # Also verify cumulative is NOT just one leg
        assert actual_cumulative > trail_exit_realized + 1, (
            "cumulative should include released leg's realized too"
        )
        assert actual_cumulative > released_near_realized + 1, (
            "cumulative should include trail exit realized too"
        )

    def test_no_double_accumulation_on_repeated_close(self, tmp_state_file):
        """Calling _write_mts_state(has_position=False) twice should NOT double-count."""
        strategy = TMFSpread()
        bar = _make_bar()

        with patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", str(tmp_state_file)):
            strategy.init(StrategyContext(
                market=MarketData(last_bar=bar, ticker="TMF"),
                position=PositionView(size=0),
                config={"ticker": "TMF", "params": {}},
            ))
            strategy._has_position = True
            strategy._lifecycle_oca = type("LC", (), {
                "phase": PositionPhase.SPREAD,
                "release_group": type("RG", (), {"status": ReleaseGroupStatus.ARMED})(),
                "trail_group": type("TG", (), {"status": TrailGroupStatus.INACTIVE})(),
            })()

            # Write a state with realized PnL from released leg
            _write_mts_state(
                has_position=True, action="OPEN", reason="test",
                near_entry=45920.0, far_entry=46076.0,
                near_last=44800.0, far_last=46076.0,
                near_side="SHORT", far_side="LONG",
                near_status="RELEASED", far_status="OPEN",
                released_leg="near", release_price=44800.0,
                trade_id="test-dedup", ticker="TMF",
                atr=80.0,
                lifecycle={"phase": "SPREAD", "release_group": {"status": "ARMED"}, "trail_group": {"status": "INACTIVE"}},
            )

        state_before = json.loads(tmp_state_file.read_text())
        cum_before = state_before["cumulative_realized_pnl"]

        # First close
        with patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", str(tmp_state_file)):
            _write_mts_state(
                has_position=False, action="CLOSE", reason="trail_exit",
                ticker="TMF",
                trail_exit_realized=18.4,
                lifecycle={"phase": "FLAT", "release_group": {"status": "INACTIVE"}, "trail_group": {"status": "INACTIVE"}},
            )

        state_after_1 = json.loads(tmp_state_file.read_text())
        cum_after_1 = state_after_1["cumulative_realized_pnl"]
        assert cum_after_1 > cum_before, "cumulative should increase on first close"

        # Second close (should NOT accumulate again)
        with patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", str(tmp_state_file)):
            _write_mts_state(
                has_position=False, action="CLOSE", reason="trail_exit",
                ticker="TMF",
                trail_exit_realized=18.4,
                lifecycle={"phase": "FLAT", "release_group": {"status": "INACTIVE"}, "trail_group": {"status": "INACTIVE"}},
            )

        state_after_2 = json.loads(tmp_state_file.read_text())
        cum_after_2 = state_after_2["cumulative_realized_pnl"]

        assert cum_after_2 == cum_after_1, (
            f"Second close should NOT change cumulative: "
            f"{cum_after_1} → {cum_after_2}"
        )

    def test_clear_records_preserves_cumulative(self, tmp_state_file):
        """clear_records (FLAT write) should preserve cumulative_realized_pnl."""
        # Pre-populate state with cumulative
        state_before = {
            "has_position": False,
            "state": "FLAT",
            "cumulative_realized_pnl": 1500.0,
            "initial_balance": 100000,
            "near_entry": 0, "far_entry": 0,
            "near_last": 0, "far_last": 0,
        }
        tmp_state_file.write_text(json.dumps(state_before))

        with patch("strategies.plugins.futures.active.tmf_spread._MTS_STATE_FILE", str(tmp_state_file)):
            _write_mts_state(
                has_position=False, action="FLAT", reason="MANUAL_CLEAR",
                ticker="TMF",
                lifecycle={"phase": "FLAT", "release_group": {"status": "INACTIVE"}, "trail_group": {"status": "INACTIVE"}},
                manual_trade_status="READY",
            )

        state_after = json.loads(tmp_state_file.read_text())
        assert state_after["cumulative_realized_pnl"] == 1500.0, (
            "clear_records must preserve cumulative_realized_pnl"
        )
        assert state_after["initial_balance"] == 100000
