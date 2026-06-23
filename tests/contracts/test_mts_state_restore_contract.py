"""
Contract: MTS state file restoration on hot-reload / restart.

P0: If the system restarts while an MTS spread position is open,
the TMFSpread strategy MUST restore _has_position and related
state from /tmp/mts_position_state.json so that position management
(RELEASE / EXIT) continues without interruption.

Guards that MUST prevent false restore:
- State file missing → no restore (fresh start)
- has_position=False → no restore (position closed)
- _updated > 1 hour → no restore (session too old, free to re-enter)
"""
import os
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import pandas as pd

from core.strategy_context import StrategyContext

_TMP_STATE = "/tmp/mts_test_restore_state.json"
_MODULE = "strategies.plugins.futures.active.tmf_spread"


def _write_test_state(**kwargs):
    """Write a test state file with sensible defaults."""
    defaults = {
        "has_position": True,
        "state": "HOLDING_SPREAD",
        "reason": "test",
        "entry_spread_z": 2.5,
        "current_spread_z": None,
        "release_state": "BOTH_HELD",
        "released_leg": None,
        "remaining_leg": None,
        "remaining_side": "LONG",
        "near_status": "OPEN",
        "near_side": "LONG",
        "near_entry": 42000.0,
        "near_last": 42050.0,
        "near_upl": 50.0,
        "near_realized_pnl": 0.0,
        "far_status": "OPEN",
        "far_side": "SHORT",
        "far_entry": 41950.0,
        "far_last": 41900.0,
        "far_upl": 50.0,
        "far_realized_pnl": 0.0,
        "total_upl": 100.0,
        "total_realized_pnl": 0.0,
        "spread_z": 2.5,
        "trail_side": None,
        "trail_mode": None,
        "trail_peak": 0.0,
        "trail_nadir": 0.0,
        "trail_stop_price": 0.0,
        "distance_to_stop": 0.0,
        "release_stop_points": 20,
        "trail_distance_points": 30,
        "trade_id": "mts-test-001",
        "_updated": datetime.now().isoformat(),
    }
    defaults.update(kwargs)
    with open(_TMP_STATE, "w") as f:
        json.dump(defaults, f)
    return defaults


def _no_spread_bar():
    """Return a bar that won't trigger entry (spread_z=0)."""
    return {
        "near_close": 42000.0,
        "far_close": 41900.0,
        "spread_z": 0.0,
        "timestamp": pd.Timestamp("2026-05-15 10:00:00"),
    }


def _neutral_context(bar=None):
    """Return a StrategyContext with given bar (default: spread_z=0)."""
    b = bar if bar is not None else _no_spread_bar()
    return StrategyContext(
        market=SimpleNamespace(last_bar=b),
        position=SimpleNamespace(size=0, entry_price=0, unrealized_pnl=0, current_stop_loss=None),
        config={"params": {}},
    )


@pytest.fixture(autouse=True)
def _clean_state_file():
    yield
    if os.path.exists(_TMP_STATE):
        os.remove(_TMP_STATE)
    # 2026-05-29 Hermes Agent: also clean fill/event logs to prevent test contamination
    _fill_log = "logs/mts_trade_fills.jsonl"
    _event_log = "logs/mts_spread_events.jsonl"
    for _f in (_fill_log, _event_log):
        if os.path.exists(_f):
            os.remove(_f)


class TestMTSStateRestore:
    """P0: Hot-reload / restart must restore open position state."""

    def test_restores_has_position_from_state_file(self):
        """State file with open position → _has_position becomes True."""
        _write_test_state()
        ctx = _neutral_context()

        with patch(f"{_MODULE}._MTS_STATE_FILE", _TMP_STATE):
            from strategies.plugins.futures.active.tmf_spread import TMFSpread

            strategy = TMFSpread()
            strategy.init(ctx)
            assert strategy._has_position is False  # fresh instance

            result = strategy.on_bar(ctx)

            # Restored!
            assert strategy._has_position is True
            assert strategy._near_entry == 42000.0
            assert strategy._far_entry == 41950.0
            assert strategy._near_side == "LONG"
            assert strategy._far_side == "SHORT"
            assert strategy._released_leg is None
            assert strategy._side == "LONG"
            assert strategy._trade_id == "mts-test-001"
            # Both legs held, no trigger → HOLDING_SPREAD (None return)
            assert result is None

    def test_restores_after_near_release(self):
        """State file with NEAR released, remaining=FAR SHORT."""
        _write_test_state(
            state="RELEASE_NEAR",
            released_leg="near",
            remaining_leg="FAR",
            remaining_side="SHORT",
            near_status="RELEASED",
            far_status="OPEN",
            near_side="SHORT",   # original entry side preserved in state
            far_side="SHORT",    # original entry side
            far_entry=41950.0,
            trail_side="SHORT",
            trail_mode="NADIR_PLUS_DISTANCE",
            trail_peak=0.0,
            trail_nadir=41950.0,
            trail_stop_price=41980.0,
            distance_to_stop=0.0,
        )
        ctx = _neutral_context()

        with patch(f"{_MODULE}._MTS_STATE_FILE", _TMP_STATE):
            from strategies.plugins.futures.active.tmf_spread import TMFSpread

            strategy = TMFSpread()
            strategy.init(ctx)
            strategy.on_bar(ctx)

            assert strategy._has_position is True
            assert strategy._released_leg == "near"
            assert strategy._side == "SHORT"
            assert strategy._near_side == "SHORT"
            assert strategy._far_side == "SHORT"
            # nadir initialized from state (41950), then _manage_position
            # updates it to min(state_nadir, far_close=41900) = 41900
            assert strategy._nadir == 41900.0

    def test_restores_after_far_release_and_exits(self):
        """
        FAR released, remaining=NEAR LONG with trail_peak > current.
        Restored instance should detect trail trigger and EXIT.
        """
        _write_test_state(
            state="RELEASE_FAR",
            released_leg="far",
            remaining_leg="NEAR",
            remaining_side="LONG",
            near_status="OPEN",
            far_status="RELEASED",
            near_side="LONG",    # original
            far_side="SHORT",    # original
            near_entry=42000.0,
            trail_side="LONG",
            trail_mode="PEAK_MINUS_DISTANCE",
            trail_peak=42100.0,  # peak set during trailing
            trail_nadir=0.0,
            trail_stop_price=42070.0,
            distance_to_stop=20.0,
        )
        # Bar: near_close=42050, peak=42100, trail 50 >= 30 → EXIT
        ctx = _neutral_context(bar={
            "near_close": 42050.0,
            "far_close": 41900.0,
            "spread_z": 2.5,
            "timestamp": pd.Timestamp("2026-05-15 10:00:00"),
        })

        with patch(f"{_MODULE}._MTS_STATE_FILE", _TMP_STATE):
            from strategies.plugins.futures.active.tmf_spread import TMFSpread

            strategy = TMFSpread()
            strategy.init(ctx)
            result = strategy.on_bar(ctx)

            # Position was restored and exited in one tick
            # Under Deferred Strategy Sync, _has_position remains True (lifecycle = EXITING) until fill confirmation calls _reset()
            # 2026-06-23 Gemini CLI: Update test to comply with Deferred Strategy Sync contract
            assert strategy._lifecycle == "EXITING"
            assert strategy._has_position is True
            strategy._reset()  # simulate confirmed fill callback from monitor
            assert strategy._has_position is False
            assert result is not None
            assert result.action == "EXIT"
            assert "TRAIL" in result.reason

    def test_does_not_restore_when_closed(self):
        """has_position=False in state → no restore, fresh start."""
        _write_test_state(has_position=False, state="CLOSE")
        ctx = _neutral_context()

        with patch(f"{_MODULE}._MTS_STATE_FILE", _TMP_STATE):
            from strategies.plugins.futures.active.tmf_spread import TMFSpread

            strategy = TMFSpread()
            strategy.init(ctx)
            assert strategy._has_position is False
            result = strategy.on_bar(ctx)
            assert strategy._has_position is False  # no position opened (spread_z=0)
            assert result is None

    def test_does_not_restore_when_stale(self):
        """State older than 1h → not restored; fresh entry still allowed."""
        _write_test_state(
            _updated=(datetime.now() - timedelta(hours=2)).isoformat()
        )
        ctx = _neutral_context()

        with patch(f"{_MODULE}._MTS_STATE_FILE", _TMP_STATE):
            from strategies.plugins.futures.active.tmf_spread import TMFSpread

            strategy = TMFSpread()
            strategy.init(ctx)
            assert strategy._has_position is False
            result = strategy.on_bar(ctx)
            # No entry because spread_z=0, so stays flat
            assert strategy._has_position is False
            assert result is None

    def test_skips_when_no_state_file(self):
        """No state file → no crash, fresh start."""
        with patch(f"{_MODULE}._MTS_STATE_FILE", "/tmp/nonexistent_mts_test.json"):
            from strategies.plugins.futures.active.tmf_spread import TMFSpread

            strategy = TMFSpread()
            strategy.init(_neutral_context())
            assert strategy._has_position is False
            result = strategy.on_bar(_neutral_context())
            assert strategy._has_position is False
            assert result is None

    def test_stale_state_does_not_block_new_entry(self):
        """Stale state file → position not restored, but normal entry still works."""
        _write_test_state(
            _updated=(datetime.now() - timedelta(hours=2)).isoformat()
        )
        ctx = _neutral_context(bar={
            "near_close": 42000.0,
            "far_close": 41600.0,
            "spread_z": 3.0,   # triggers entry
            "timestamp": pd.Timestamp("2026-05-15 10:00:00"),
        })

        with patch(f"{_MODULE}._MTS_STATE_FILE", _TMP_STATE):
            from strategies.plugins.futures.active.tmf_spread import TMFSpread

            strategy = TMFSpread()
            strategy.init(ctx)
            assert strategy._has_position is False
            result = strategy.on_bar(ctx)
            # Fresh entry succeeds (stale state doesn't block)
            # 2026-05-25 Gemini CLI: Updated for Deferred Strategy Sync
            assert result is not None
            assert result.action in ("SELL_NEAR_BUY_FAR", "BUY_NEAR_SELL_FAR")
            assert strategy._lifecycle == "SUBMITTING"
            assert strategy._has_position is False  # Awaiting fill confirmation

    def test_back_to_back_restore_is_idempotent(self):
        """Second on_bar after restore doesn't re-restore or crash."""
        _write_test_state()
        ctx = _neutral_context()

        with patch(f"{_MODULE}._MTS_STATE_FILE", _TMP_STATE):
            from strategies.plugins.futures.active.tmf_spread import TMFSpread

            strategy = TMFSpread()
            strategy.init(ctx)
            strategy.on_bar(ctx)   # first: restores
            assert strategy._has_position is True
            # second: _has_position is already True, skip restore
            result2 = strategy.on_bar(ctx)
            assert strategy._has_position is True
