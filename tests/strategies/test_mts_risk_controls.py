"""
Unit tests for MTS risk control gates.

2026-07-08 Hermes Agent:
- SINGLE_LEG pre-close force flat (within 5 min of session close)
- Settlement day force flat (after 13:30)
- Idempotency: no duplicate exit on repeated ticks
"""
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, PropertyMock

from strategies.plugins.futures.active.tmf_spread import (
    PositionPhase, PositionLifecycle, ReleaseGroup, ReleaseGroupStatus,
    TrailGroup, TrailGroupStatus, Leg,
)


def _make_mock_strategy(phase=PositionPhase.SPREAD, released_leg=None, side=None,
                        has_position=True, lifecycle_str="OPEN"):
    """Build a mock strategy with the given state."""
    s = MagicMock()
    s._has_position = has_position
    s._lifecycle = lifecycle_str
    s._released_leg = released_leg
    s._side = side
    s._near_side = "SHORT"
    s._far_side = "LONG"
    s._lifecycle_oca = PositionLifecycle(
        phase=phase,
        release_group=ReleaseGroup(
            status=ReleaseGroupStatus.FILLED if released_leg else ReleaseGroupStatus.ARMED,
            filled_leg=Leg.NEAR if released_leg == "near" else (Leg.FAR if released_leg == "far" else None),
        ),
        trail_group=TrailGroup(
            status=TrailGroupStatus.ARMED if released_leg else TrailGroupStatus.INACTIVE,
            remaining_leg=Leg.FAR if released_leg == "near" else (Leg.NEAR if released_leg == "far" else None),
        ),
    )
    return s


class TestSingleLegPrecloseGate:
    """SINGLE_LEG pre-close force flat: triggers only in SINGLE_LEG phase, within 5min."""

    def test_single_leg_within_5min_triggers(self):
        """SINGLE_LEG + <5min to close → gate returns True, exit submitted."""
        from strategies.futures.monitor import FuturesMonitor
        from core.strategy_context import StrategyContext, MarketData, PositionView

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor._is_contract_expired = MagicMock(return_value=False)
        monitor._submit_mts_order_signal = MagicMock()
        monitor._mts_force_exit_inflight = False

        strategy = _make_mock_strategy(
            phase=PositionPhase.SINGLE_LEG, released_leg="near", side="LONG",
        )

        bar_dict = {"near_close": 45000, "far_close": 45800, "atr": 80}

        # Mock time: 13:40 (5 min before day close at 13:45)
        mock_dt = datetime(2026, 7, 8, 13, 40, 0)
        with patch("core.date_utils.datetime") as mock_dt_mod:
            mock_dt_mod.now.return_value = mock_dt
            result = monitor._mts_risk_gate_single_leg_preclose(strategy, bar_dict)

        assert result is True
        assert monitor._mts_force_exit_inflight is True
        monitor._submit_mts_order_signal.assert_called_once()
        call_args = monitor._submit_mts_order_signal.call_args
        signal = call_args[0][0]
        assert signal.action == "EXIT"
        assert "SESSION_CLOSE_FORCE" in str(signal.reason)

    def test_single_leg_outside_5min_does_not_trigger(self):
        """SINGLE_LEG + >5min to close → gate returns False."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor._is_contract_expired = MagicMock(return_value=False)

        strategy = _make_mock_strategy(
            phase=PositionPhase.SINGLE_LEG, released_leg="near", side="LONG",
        )
        bar_dict = {"near_close": 45000, "far_close": 45800, "atr": 80}

        # 13:30 = 15 min before close
        mock_dt = datetime(2026, 7, 8, 13, 30, 0)
        with patch("core.date_utils.datetime") as mock_dt_mod:
            mock_dt_mod.now.return_value = mock_dt
            result = monitor._mts_risk_gate_single_leg_preclose(strategy, bar_dict)

        assert result is False

    def test_spread_phase_does_not_trigger(self):
        """SPREAD phase → gate returns False, even within 5min."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor._is_contract_expired = MagicMock(return_value=False)

        strategy = _make_mock_strategy(
            phase=PositionPhase.SPREAD, released_leg=None, side=None,
        )
        bar_dict = {"near_close": 45000, "far_close": 45800, "atr": 80}

        mock_dt = datetime(2026, 7, 8, 13, 40, 0)
        with patch("core.date_utils.datetime") as mock_dt_mod:
            mock_dt_mod.now.return_value = mock_dt
            result = monitor._mts_risk_gate_single_leg_preclose(strategy, bar_dict)

        assert result is False

    def test_flat_does_not_trigger(self):
        """FLAT phase → gate returns False."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor._is_contract_expired = MagicMock(return_value=False)

        strategy = _make_mock_strategy(
            phase=PositionPhase.FLAT, released_leg=None, side=None,
            has_position=False, lifecycle_str="FLAT",
        )
        bar_dict = {"near_close": 45000, "far_close": 45800, "atr": 80}

        mock_dt = datetime(2026, 7, 8, 13, 40, 0)
        with patch("core.date_utils.datetime") as mock_dt_mod:
            mock_dt_mod.now.return_value = mock_dt
            result = monitor._mts_risk_gate_single_leg_preclose(strategy, bar_dict)

        assert result is False

    def test_idempotency_inflight_skips(self):
        """When _mts_force_exit_inflight is True, gate returns True but does NOT submit."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor._is_contract_expired = MagicMock(return_value=False)
        monitor._submit_mts_order_signal = MagicMock()
        monitor._mts_force_exit_inflight = True  # already in flight

        strategy = _make_mock_strategy(
            phase=PositionPhase.SINGLE_LEG, released_leg="near", side="LONG",
        )
        bar_dict = {"near_close": 45000, "far_close": 45800, "atr": 80}

        mock_dt = datetime(2026, 7, 8, 13, 40, 0)
        with patch("core.date_utils.datetime") as mock_dt_mod:
            mock_dt_mod.now.return_value = mock_dt
            result = monitor._mts_risk_gate_single_leg_preclose(strategy, bar_dict)

        assert result is True  # gate handled (skip on_bar), but...
        monitor._submit_mts_order_signal.assert_not_called()  # no duplicate

    def test_exiting_already_does_not_fire(self):
        """When strategy._lifecycle == 'EXITING', gate returns False (no double-fire)."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor._is_contract_expired = MagicMock(return_value=False)

        strategy = _make_mock_strategy(
            phase=PositionPhase.SINGLE_LEG, released_leg="near", side="LONG",
            lifecycle_str="EXITING",
        )
        bar_dict = {"near_close": 45000, "far_close": 45800, "atr": 80}

        mock_dt = datetime(2026, 7, 8, 13, 40, 0)
        with patch("core.date_utils.datetime") as mock_dt_mod:
            mock_dt_mod.now.return_value = mock_dt
            result = monitor._mts_risk_gate_single_leg_preclose(strategy, bar_dict)

        assert result is False

    def test_missing_released_leg_does_not_trigger(self):
        """SINGLE_LEG but _released_leg is None → gate returns False."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor._is_contract_expired = MagicMock(return_value=False)

        strategy = _make_mock_strategy(
            phase=PositionPhase.SINGLE_LEG, released_leg=None, side=None,
        )
        bar_dict = {"near_close": 45000, "far_close": 45800, "atr": 80}

        mock_dt = datetime(2026, 7, 8, 13, 40, 0)
        with patch("core.date_utils.datetime") as mock_dt_mod:
            mock_dt_mod.now.return_value = mock_dt
            result = monitor._mts_risk_gate_single_leg_preclose(strategy, bar_dict)

        assert result is False


class TestSettlementGate:
    """Settlement day force flat: all phases, full close after 13:30."""

    def test_settlement_day_triggers_force_flat(self):
        """Contract expired + has position → gate returns True, emergency flatten called."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor.contract.code = "TMFG6"
        # 2026-07-09 Hermes Agent: use dynamic current date to prevent datetime patch mismatch
        monitor.contract.delivery_date = datetime.now().strftime("%Y/%m/%d")
        monitor._is_contract_expired = MagicMock(return_value=True)
        monitor._emergency_flatten_mts = MagicMock()

        strategy = _make_mock_strategy(
            phase=PositionPhase.SPREAD, has_position=True,
        )

        result = monitor._mts_risk_gate_settlement(strategy)

        assert result is True
        monitor._emergency_flatten_mts.assert_called_once_with(strategy)

    def test_settlement_day_no_position_does_not_trigger(self):
        """Contract expired but no position → gate returns False."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor.contract.code = "TMFG6"
        monitor.contract.delivery_date = "2026/07/08"
        monitor._is_contract_expired = MagicMock(return_value=True)

        strategy = _make_mock_strategy(
            phase=PositionPhase.FLAT, has_position=False,
        )

        result = monitor._mts_risk_gate_settlement(strategy)

        assert result is False

    def test_not_expired_does_not_trigger(self):
        """Contract not expired → gate returns False."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor.contract.code = "TMFH6"
        monitor.contract.delivery_date = "2026/08/19"
        monitor._is_contract_expired = MagicMock(return_value=False)

        strategy = _make_mock_strategy(
            phase=PositionPhase.SINGLE_LEG, has_position=True,
            released_leg="near", side="LONG",
        )

        result = monitor._mts_risk_gate_settlement(strategy)

        assert result is False

    def test_settlement_single_leg_also_triggered(self):
        """SINGLE_LEG on settlement day → force flat still triggers."""
        from strategies.futures.monitor import FuturesMonitor

        monitor = FuturesMonitor.__new__(FuturesMonitor)
        monitor.contract = MagicMock()
        monitor.contract.code = "TMFG6"
        # 2026-07-09 Hermes Agent: use dynamic current date to prevent datetime patch mismatch
        monitor.contract.delivery_date = datetime.now().strftime("%Y/%m/%d")
        monitor._is_contract_expired = MagicMock(return_value=True)
        monitor._emergency_flatten_mts = MagicMock()

        strategy = _make_mock_strategy(
            phase=PositionPhase.SINGLE_LEG, has_position=True,
            released_leg="near", side="LONG",
        )

        result = monitor._mts_risk_gate_settlement(strategy)

        assert result is True
        monitor._emergency_flatten_mts.assert_called_once()
