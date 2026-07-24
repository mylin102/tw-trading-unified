"""
Contract test: Ghost EXIT blocked when position authority is FLAT.

2026-07-07 Hermes Agent: Verifies that MTS_EXIT orders are NOT created
when the persistent state file says has_position=false, even if the
strategy runtime flag _has_position=True (desync scenario).
"""
import json
import os
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from types import SimpleNamespace

from core.order_management.order_manager import OrderManager
from core.order_management.order import Order, OrderSide, OrderType, OrderStatus
from core.order_management.paper_fill import PaperFillSimulator


class TestGhostExitBlockedWhenAuthorityFlat:
    """P0: Position authority gate prevents ghost MTS_EXIT orders."""

    @pytest.fixture
    def setup(self, tmp_path, monkeypatch):
        """Setup: strategy has _has_position=True but state file says FLAT."""
        from strategies.futures.monitor import FuturesMonitor

        # Isolated state path
        state_path = tmp_path / "mts_position_state.json"
        monkeypatch.setenv("MTS_STATE_PATH", str(state_path))

        # Write FLAT state
        state_path.write_text(json.dumps({
            "has_position": False,
            "state": "FLAT",
            "near_side": None,
            "far_side": None,
            "lifecycle": {"phase": "FLAT", "release_group": {"status": "INACTIVE"}, "trail_group": {"status": "INACTIVE"}},
        }))

        # Build minimal monitor
        m = FuturesMonitor.__new__(FuturesMonitor)
        om = OrderManager(mode="paper")
        sim = PaperFillSimulator(om)

        object.__setattr__(m, "order_mgr", om)
        object.__setattr__(m, "paper_fill_sim", sim)
        object.__setattr__(m, "ticker", "TMF")
        object.__setattr__(m, "contract", SimpleNamespace(code="TMFG6"))
        object.__setattr__(m, "far_contract", SimpleNamespace(code="TMFH6"))
        object.__setattr__(m, "_registry", {"tmf_spread": None})
        object.__setattr__(m, "cfg", {"mts": {"strategy": "tmf_spread"}})
        object.__setattr__(m, "trader", SimpleNamespace(position=0, entry_price=0.0))
        object.__setattr__(m, "market_data", {})

        exports_dir = tmp_path / "exports" / "trades"
        exports_dir.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(tmp_path)

        return m, om, sim, state_path

    def test_exit_rejected_when_authority_flat(self, setup):
        """State file says FLAT → _submit_mts_order_signal rejects EXIT."""
        m, om, sim, state_path = setup

        # Create a mock signal with EXIT action
        signal = SimpleNamespace(action="EXIT", reason="TRAIL_STOP")
        strategy = SimpleNamespace(_has_position=True)  # desync!

        bar_dict = {
            "near_close": 45700, "far_close": 46000,
            "near_tick_age_ms": 50, "far_tick_age_ms": 60,
        }

        from datetime import datetime
        from strategies.futures.monitor import FuturesMonitor
        result = FuturesMonitor._submit_mts_order_signal(
            m, signal, strategy, bar_dict, datetime.now()
        )

        # Should return None (blocked) — no order created
        assert result is None
        assert len(om.active_orders) == 0
        assert len(om.completed) == 0

    def test_partial_exit_rejected_when_authority_flat(self, setup):
        """State file says FLAT → _submit_mts_order_signal rejects PARTIAL_EXIT."""
        m, om, sim, state_path = setup

        signal = SimpleNamespace(action="PARTIAL_EXIT", reason="RELEASE_NEAR")
        strategy = SimpleNamespace(
            _has_position=True, _released_leg="near",
            _near_side="LONG", _far_side="SHORT",
        )

        bar_dict = {
            "near_close": 45700, "far_close": 46000,
            "near_tick_age_ms": 50, "far_tick_age_ms": 60,
        }

        from datetime import datetime
        from strategies.futures.monitor import FuturesMonitor
        result = FuturesMonitor._submit_mts_order_signal(
            m, signal, strategy, bar_dict, datetime.now()
        )

        assert result is None
        assert len(om.active_orders) == 0
