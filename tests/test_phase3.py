#!/usr/bin/env python3
"""Tests for Phase 3: Monitor Integration + Dashboard Pipeline."""
import os
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.circuit_breaker import CircuitBreaker, Action
from core.diagnostic_engine import diagnose_losing_streak, TradeDiagnosis
from core.decision_logger import DecisionLogger


# ─── Monitor Integration ────────────────────────────────────────────────

class TestMonitorIntegration:
    """Verify that FuturesMonitor has all Phase 1-3 components integrated."""

    def test_monitor_imports_ok(self):
        from strategies.futures.monitor import FuturesMonitor
        assert FuturesMonitor is not None

    def test_monitor_has_circuit_breaker_attr(self):
        """Verify __init__ sets up circuit breaker placeholder."""
        import inspect
        from strategies.futures.monitor import FuturesMonitor
        src = inspect.getsource(FuturesMonitor.__init__)
        assert '_circuit_breaker' in src
        assert '_session_pnl' in src

    def test_monitor_has_consecutive_losses(self):
        import inspect
        from strategies.futures.monitor import FuturesMonitor
        src = inspect.getsource(FuturesMonitor.__init__)
        assert 'consecutive_losses' in src

    def test_monitor_has_session_losses(self):
        import inspect
        from strategies.futures.monitor import FuturesMonitor
        src = inspect.getsource(FuturesMonitor.__init__)
        assert 'session_losses' in src

    def test_monitor_has_bar_context(self):
        import inspect
        from strategies.futures.monitor import FuturesMonitor
        src = inspect.getsource(FuturesMonitor.__init__)
        assert '_last_bar_context' in src

    def test_monitor_has_hourly_audit(self):
        import inspect
        from strategies.futures.monitor import FuturesMonitor
        assert hasattr(FuturesMonitor, '_hourly_no_trade_audit')

    def test_monitor_tracks_options_monitor_for_hourly_repair(self):
        import inspect
        from strategies.futures.monitor import FuturesMonitor
        src = inspect.getsource(FuturesMonitor.__init__)
        assert 'self.options_monitor = None' in src

    def test_monitor_tick_checks_breaker(self):
        """Verify _strategy_tick has circuit breaker check."""
        import inspect
        from strategies.futures.monitor import FuturesMonitor
        src = inspect.getsource(FuturesMonitor._strategy_tick)
        assert '_circuit_breaker' in src


# ─── Integration: Diagnostic + Decision Logger ──────────────────────────

class TestPhase3Integration:
    """Test that Phase 1+2+3 components work together."""

    def test_main_wires_options_monitor_into_futures_audit(self):
        src = Path("main.py").read_text()
        assert "fm.options_monitor = om.monitor" in src

    def test_hourly_audit_calls_options_repair(self):
        from strategies.futures.monitor import FuturesMonitor
        import inspect
        src = inspect.getsource(FuturesMonitor._hourly_no_trade_audit)
        assert "_audit_options_data_health" in src

    def test_options_monitor_exposes_data_repair_audit(self):
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "def audit_indicator_health_and_repair" in src
        assert "def _select_live_bar_frames" in src

    def test_diagnostic_logs_decision(self, tmp_path):
        """When diagnostic returns action, it gets logged."""
        from core.decision_logger import DecisionLogger

        # Simulate a diagnostic action being logged
        DecisionLogger.log(
            type="circuit_breaker", session="day",
            action="diagnose", detail="3 consecutive losses",
            path=tmp_path / "test_decisions.csv",
        )

        decisions = DecisionLogger.read(path=tmp_path / "test_decisions.csv")
        assert len(decisions) == 1
        assert decisions[0].type == "circuit_breaker"
        assert decisions[0].action == "diagnose"

    def test_circuit_breaker_halts_and_logs(self, tmp_path):
        """When breaker halts, it should log the decision."""
        from core.circuit_breaker import CircuitBreaker
        breaker = CircuitBreaker(session="day", daily_loss_cap=5000)
        action = breaker.check(pnl=-6000, consecutive_losses=0)
        assert action == Action.HALT
        assert breaker.is_halted

        # Log the halt
        DecisionLogger.log(
            type="circuit_breaker", session="day",
            action="halt", detail="Daily loss cap",
            path=tmp_path / "test_decisions.csv",
        )

        decisions = DecisionLogger.read(path=tmp_path / "test_decisions.csv")
        assert any(d.action == "halt" for d in decisions)

    def test_full_diagnostic_flow(self, tmp_path):
        """Entry diagnostic + exit reason → diagnostic engine → decision logged."""
        # Simulate a losing trade with entry diagnostic
        trades = [
            TradeDiagnosis(
                exit_reason="STOP_LOSS",
                pnl_pts=-60,
                entry_diag={"momentum": 15, "vwap_distance_pts": 150, "atr": 50, "regime": "trending", "session": "day"},
                session="day",
            ),
            TradeDiagnosis(
                exit_reason="STOP_LOSS",
                pnl_pts=-55,
                entry_diag={"momentum": 20, "vwap_distance_pts": 120, "atr": 50, "regime": "trending", "session": "day"},
                session="day",
            ),
            TradeDiagnosis(
                exit_reason="STOP_LOSS",
                pnl_pts=-65,
                entry_diag={"momentum": 18, "vwap_distance_pts": 130, "atr": 50, "regime": "trending", "session": "day"},
                session="day",
            ),
        ]

        # Run diagnostic
        action = diagnose_losing_streak(trades)
        assert action.action_type == "TIGHTEN_ENTRY"
        assert action.param == "confirm_bars"

        # Log the decision
        DecisionLogger.log(
            type="post_session", session="day",
            action="tighten_entry", detail=action.reason,
            path=tmp_path / "test_decisions.csv",
        )

        decisions = DecisionLogger.read(path=tmp_path / "test_decisions.csv")
        assert decisions[0].action == "tighten_entry"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
