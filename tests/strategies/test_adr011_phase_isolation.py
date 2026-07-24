"""
ADR-011: Phase Isolation Guard Tests.

Verifies that MTS_EXIT is blocked unless lifecycle.phase == SINGLE_LEG
AND release_group.status == FILLED.

2026-07-16: Regression guard for 38ms double-order bug.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from strategies.plugins.futures.active.tmf_spread import (
    PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
    TrailGroup, TrailGroupStatus, Leg,
)
from core.signal import Signal


@pytest.fixture(autouse=True)
def mock_market_open():
    with patch("strategies.futures.monitor.is_taifex_futures_market_open", return_value=True):
        yield

@pytest.fixture
def monitor():
    from strategies.futures.monitor import FuturesMonitor
    api = MagicMock()
    api.Contracts.Futures.TMF = [MagicMock(code="TMFF6")]
    m = FuturesMonitor(api, "config/futures_night.yaml", dry_run=True)
    m.ticker = "TMF"
    m._use_order_manager = True
    from core.order_management.order_manager import OrderManager
    m.order_mgr = OrderManager(api)
    m.contract = MagicMock(code="TMFF6")
    m.far_contract = MagicMock(code="TMFH6")
    return m


@pytest.fixture
def strategy():
    from strategies.plugins.futures.active.tmf_spread import TMFSpread
    s = TMFSpread()
    s._has_position = True
    s._released_leg = "far"
    s._near_side = "LONG"
    s._far_side = "SHORT"
    s._side = "LONG"
    s._near_entry = 45384.0
    s._far_entry = 45708.0
    s._peak = 45900.0
    s._ticker = "TMF"
    s._trade_id = "adr011-test"
    return s


@pytest.fixture
def bar_dict():
    from datetime import datetime
    return {
        "near_close": 45800.0, "far_close": 45600.0, "atr": 10.0,
        "timestamp": datetime.now(),
    }


# ── Test 1: SPREAD + ARMED → MTS_EXIT blocked ──

def test_exit_blocked_when_spread_armed(monitor, strategy, bar_dict):
    """
    Phase=SPREAD, rg_status=ARMED: no release fill yet.
    MTS_EXIT must be blocked.
    """
    from datetime import datetime
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.ARMED),
    )
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert not mock_submit.called, (
            "MTS_EXIT blocked: phase=SPREAD rg_status=ARMED"
        )


# ── Test 2: SPREAD + SUBMITTED → MTS_EXIT blocked ──

def test_exit_blocked_when_spread_submitted(monitor, strategy, bar_dict):
    """
    Phase=SPREAD, rg_status=SUBMITTED: release order sent but not yet filled.
    MTS_EXIT must be blocked.
    """
    from datetime import datetime
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.SUBMITTED,
            near_order_id="ORD-ADR011-001",
            far_order_id="ORD-ADR011-002",
        ),
    )
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert not mock_submit.called, (
            "MTS_EXIT blocked: phase=SPREAD rg_status=SUBMITTED"
        )


# ── Test 3: SPREAD + TRIGGERED → MTS_EXIT blocked ──

def test_exit_blocked_when_spread_triggered(monitor, strategy, bar_dict):
    """
    Phase=SPREAD, rg_status=TRIGGERED: release threshold hit but order not submitted yet.
    MTS_EXIT must be blocked.
    """
    from datetime import datetime
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(status=ReleaseGroupStatus.TRIGGERED),
    )
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert not mock_submit.called, (
            "MTS_EXIT blocked: phase=SPREAD rg_status=TRIGGERED"
        )


# ── Test 4: SINGLE_LEG + COMPLETED → MTS_EXIT allowed (standard path) ──

def test_exit_allowed_when_single_leg_completed(monitor, strategy, bar_dict):
    """
    Phase=SINGLE_LEG, rg_status=COMPLETED: release fill confirmed by
    sync_release() (standard ARMED trigger model path).
    MTS_EXIT must be allowed.
    """
    from datetime import datetime
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.COMPLETED,
            filled_leg=Leg.FAR,
            canceled_leg=Leg.NEAR,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED, remaining_leg=Leg.NEAR),
    )
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert mock_submit.called, (
            "MTS_EXIT allowed: phase=SINGLE_LEG rg_status=COMPLETED"
        )


# ── Test 5: SINGLE_LEG + FILLED → MTS_EXIT allowed (old OCO bracket path) ──

def test_exit_allowed_when_single_leg_filled(monitor, strategy, bar_dict):
    """
    Phase=SINGLE_LEG, rg_status=FILLED: old OCO bracket model path.
    MTS_EXIT must also be allowed.
    """
    from datetime import datetime
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.FILLED,
            filled_leg=Leg.FAR,
            canceled_leg=Leg.NEAR,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED, remaining_leg=Leg.NEAR),
    )
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert mock_submit.called, (
            "MTS_EXIT allowed: phase=SINGLE_LEG rg_status=FILLED"
        )


# ── Test 6: SINGLE_LEG + SIBLING_CANCELED (old status) → blocked ──

def test_exit_blocked_when_single_leg_sibling_canceled(monitor, strategy, bar_dict):
    """
    Phase=SINGLE_LEG but rg_status=SIBLING_CANCELED (old ADR-010 model).
    New guard requires FILLED.  This tests that old status values don't
    accidentally pass the guard.
    """
    from datetime import datetime
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.SIBLING_CANCELED,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED),
    )
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert not mock_submit.called, (
            "MTS_EXIT blocked: phase=SINGLE_LEG rg_status=SIBLING_CANCELED != FILLED"
        )


# ── Test 6: Regression — MTS_RELEASE then MTS_EXIT before LEG_FILLED ──

def test_exit_blocked_when_release_pending(monitor, strategy, bar_dict):
    """
    Regression test for the 38ms double-order bug.
    Scenario:
      1. MTS_RELEASE submitted (paper mode, synchronous fill)
      2. In same evaluation cycle, MTS_EXIT is attempted
      3. If release fill callback has NOT yet transitioned to SINGLE_LEG + FILLED,
         EXIT must be blocked.

    This tests the HARDEST case: paper mode where fill IS synchronous,
    but the guard still validates lifecycle state at the submission boundary.
    """
    from datetime import datetime
    # Simulate state BEFORE paper_fill_sim fires:
    #   phase=SPREAD, rg_status=SUBMITTING (release submitted, not yet confirmed)
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SPREAD,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.SUBMITTING,
        ),
    )
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert not mock_submit.called, (
            "MTS_EXIT blocked before release fill confirmed"
        )
