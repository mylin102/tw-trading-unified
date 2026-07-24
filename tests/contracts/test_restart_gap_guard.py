"""
Commit 2 regression: restart reconciliation gap guard.

Tests the guard in _submit_mts_order_signal that blocks legacy
MTS_EXIT when lifecycle is None/FLAT but strategy has position.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from strategies.plugins.futures.active.tmf_spread import (
    PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
    TrailGroup, TrailGroupStatus, Leg,
)
from core.signal import Signal


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
    s._released_leg = "near"
    s._near_side = "SHORT"
    s._far_side = "LONG"
    s._side = "LONG"
    s._far_entry = 44000.0
    s._peak = 44100.0
    s._ticker = "TMF"
    s._trade_id = "gap-guard-test"
    return s


@pytest.fixture
def bar_dict():
    from datetime import datetime
    return {
        "near_close": 44100.0, "far_close": 44060.0, "atr": 10.0,
        "timestamp": datetime.now(),
    }


def test_blocks_exit_when_lifecycle_none_but_has_position(monitor, strategy, bar_dict):
    """phase=None + has_position=True → block."""
    from datetime import datetime
    strategy._lifecycle_oca = None
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert not mock_submit.called, "Expected block: lifecycle=None + has_position"


def test_blocks_exit_when_lifecycle_flat_but_has_position(monitor, strategy, bar_dict):
    """phase=FLAT + has_position=True → block."""
    from datetime import datetime
    strategy._lifecycle_oca = PositionLifecycle(phase=PositionPhase.FLAT)
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert not mock_submit.called, "Expected block: lifecycle=FLAT + has_position"


def test_allows_exit_when_lifecycle_single_leg(monitor, strategy, bar_dict):
    """phase=SINGLE_LEG + rg_status=COMPLETED → allow (sync_release sets COMPLETED)."""
    from datetime import datetime
    strategy._lifecycle_oca = PositionLifecycle(
        phase=PositionPhase.SINGLE_LEG,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.COMPLETED,
            filled_leg=Leg.NEAR, canceled_leg=Leg.FAR,
        ),
        trail_group=TrailGroup(status=TrailGroupStatus.ARMED),
    )
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch("strategies.futures.monitor.is_taifex_futures_market_open", return_value=True), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert mock_submit.called, "Expected allow: lifecycle=SINGLE_LEG + has_position"


def test_no_effect_when_flat_no_position(monitor, strategy, bar_dict):
    """phase=FLAT + has_position=False → EXIT blocked by Phase 1 guard."""
    from datetime import datetime
    strategy._has_position = False
    strategy._lifecycle_oca = PositionLifecycle(phase=PositionPhase.FLAT)
    signal = Signal("EXIT", "TMF_TRAIL_EXIT_LONG")

    with patch("strategies.futures.monitor._mts_position_state_path", return_value=Path("nonexistent.json")), \
         patch.object(monitor.order_mgr, "submit") as mock_submit:
        monitor._submit_mts_order_signal(signal, strategy, bar_dict, datetime.now())
        assert not mock_submit.called, (
            "ADR-011 guard blocks EXIT: phase=FLAT != SINGLE_LEG"
        )
