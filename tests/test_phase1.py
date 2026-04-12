#!/usr/bin/env python3
"""Tests for Phase 1: Decision Logger, Strategy Registry, Circuit Breaker."""
import os
import sys
import pytest
import csv
from pathlib import Path
from datetime import date

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.decision_logger import DecisionLogger, Decision, _HEADERS
from core.circuit_breaker import CircuitBreaker, Action, BreakerState
from core.strategy_registry import (
    STRATEGY_PERF,
    REGIME_STRATEGY_ORDER,
    select_best_strategy,
    get_strategy_ranking,
    DEFAULT_MIN_PF,
)


# ─── Decision Logger ─────────────────────────────────────────────────────

class TestDecisionLogger:
    def test_log_creates_file(self, tmp_path):
        path = tmp_path / "test_decisions.csv"
        d = DecisionLogger.log(
            type="test", session="day", action="test_action",
            detail="test", path=path,
        )
        assert path.exists()
        assert d.type == "test"
        assert d.action == "test_action"

    def test_log_is_append_only(self, tmp_path):
        path = tmp_path / "test_decisions.csv"
        DecisionLogger.log(type="t1", session="day", action="a1", detail="d1", path=path)
        DecisionLogger.log(type="t2", session="night", action="a2", detail="d2", path=path)

        rows = DecisionLogger.read(path=path)
        assert len(rows) == 2
        assert rows[0].action == "a2"  # newest first
        assert rows[1].action == "a1"

    def test_log_headers(self, tmp_path):
        path = tmp_path / "test_decisions.csv"
        DecisionLogger.log(type="test", session="day", action="a", detail="d", path=path)
        with open(path) as f:
            reader = csv.reader(f)
            headers = next(reader)
        assert headers == _HEADERS

    def test_read_by_session(self, tmp_path):
        path = tmp_path / "test_decisions.csv"
        DecisionLogger.log(type="test", session="day", action="a1", detail="d1", path=path)
        DecisionLogger.log(type="test", session="night", action="a2", detail="d2", path=path)
        DecisionLogger.log(type="test", session="day", action="a3", detail="d3", path=path)

        day_decisions = DecisionLogger.read_by_session("day", path=path)
        assert all(d.session == "day" for d in day_decisions)
        assert len(day_decisions) == 2

    def test_default_path(self, tmp_path):
        """Ensure default path points to logs/decisions.csv."""
        from core.decision_logger import _DECISIONS_PATH
        assert "decisions.csv" in str(_DECISIONS_PATH)

    def test_risk_level_default(self, tmp_path):
        path = tmp_path / "test_decisions.csv"
        d = DecisionLogger.log(type="test", session="day", action="a", detail="d", path=path)
        assert d.risk_level == "low"

    def test_author_default(self, tmp_path):
        path = tmp_path / "test_decisions.csv"
        d = DecisionLogger.log(type="test", session="day", action="a", detail="d", path=path)
        assert d.author == "system"


# ─── Strategy Registry (Performance Table) ──────────────────────────────

class TestStrategyRegistry:
    def test_perf_data_exists(self):
        assert len(STRATEGY_PERF) >= 3
        assert "counter_vwap" in STRATEGY_PERF

    def test_day_night_separation(self):
        for name, perf in STRATEGY_PERF.items():
            assert "day_pf" in perf, f"{name} missing day_pf"
            assert "night_pf" in perf, f"{name} missing night_pf"

    def test_select_best_day_trending(self):
        result = select_best_strategy("day", regime="trending")
        assert result == "counter_vwap"  # PF=2.1, highest in trending

    def test_select_best_night_ranging(self):
        result = select_best_strategy("night", regime="ranging")
        assert result == "counter_vwap"  # PF=1.4, still best even in ranging at night

    def test_select_best_shock_returns_fallback(self):
        result = select_best_strategy("day", regime="shock")
        # shock has empty order, falls back to all strategies scan
        assert result == "counter_vwap"

    def test_select_best_invalid_session(self):
        result = select_best_strategy("invalid_session")
        # Should fallback gracefully
        assert isinstance(result, str)
        assert len(result) > 0

    def test_get_strategy_ranking_day(self):
        ranking = get_strategy_ranking("day")
        assert len(ranking) >= 3
        assert ranking[0] == ("counter_vwap", 2.1)
        # Sorted descending
        assert all(ranking[i][1] >= ranking[i+1][1] for i in range(len(ranking)-1))

    def test_get_strategy_ranking_night_excludes_psar(self):
        ranking = get_strategy_ranking("night")
        names = [r[0] for r in ranking]
        assert "psar" not in names  # PF=0.9 < DEFAULT_MIN_PF=1.0

    def test_get_strategy_ranking_with_custom_min_pf(self):
        ranking = get_strategy_ranking("day", min_pf=1.5)
        names = [r[0] for r in ranking]
        # counter_vwap=2.1, spring_upthrust=1.6, vol_squeeze=1.5 all pass
        assert set(names) == {"counter_vwap", "spring_upthrust", "vol_squeeze"}

    def test_regime_order_has_all_strategies(self):
        all_strats = set(STRATEGY_PERF.keys())
        for regime, order in REGIME_STRATEGY_ORDER.items():
            if regime == "shock":
                continue
            for s in order:
                assert s in all_strats, f"{s} in {regime} order not in STRATEGY_PERF"


# ─── Circuit Breaker ────────────────────────────────────────────────────

class TestCircuitBreaker:
    def test_continue_on_healthy(self):
        b = CircuitBreaker(session="day", daily_loss_cap=5000)
        action = b.check(pnl=1000, consecutive_losses=0)
        assert action == Action.CONTINUE

    def test_diagnose_on_consecutive_losses(self):
        b = CircuitBreaker(session="day", daily_loss_cap=5000)
        action = b.check(pnl=-500, consecutive_losses=3)
        assert action == Action.DIAGNOSE

    def test_halt_on_daily_loss_cap(self):
        b = CircuitBreaker(session="day", daily_loss_cap=5000)
        action = b.check(pnl=-6000, consecutive_losses=0)
        assert action == Action.HALT

    def test_reduce_size_on_partial_loss(self):
        b = CircuitBreaker(session="day", daily_loss_cap=5000)
        action = b.check(pnl=-2500, consecutive_losses=1)
        assert action == Action.REDUCE_SIZE

    def test_halt_persists_same_day(self):
        b = CircuitBreaker(session="day", daily_loss_cap=5000)
        b.check(pnl=-6000, consecutive_losses=0)
        assert b.is_halted
        assert b.check(pnl=0, consecutive_losses=0) == Action.HALT

    def test_reset_clears_halt(self):
        b = CircuitBreaker(session="day", daily_loss_cap=5000)
        b.check(pnl=-6000, consecutive_losses=0)
        assert b.is_halted
        b.reset()
        assert not b.is_halted
        assert b.check(pnl=0, consecutive_losses=0) == Action.CONTINUE

    def test_day_night_independence(self):
        day_b = CircuitBreaker(session="day", daily_loss_cap=5000)
        night_b = CircuitBreaker(session="night", daily_loss_cap=5000)

        # Day halts
        day_b.check(pnl=-6000, consecutive_losses=0)
        assert day_b.is_halted

        # Night should NOT be affected
        assert not night_b.is_halted
        assert night_b.check(pnl=100, consecutive_losses=0) == Action.CONTINUE

    def test_state_snapshot(self):
        b = CircuitBreaker(session="night", daily_loss_cap=3000)
        b.check(pnl=-1500, consecutive_losses=2)
        state = b.state
        assert state.session_pnl == -1500
        assert state.consecutive_losses == 2
        assert state.daily_loss_cap == 3000

    def test_diagnose_priority_over_halt_for_consecutive(self):
        """When consecutive_losses >= max_consecutive, DIAGNOSE fires first."""
        b = CircuitBreaker(session="day", daily_loss_cap=5000)
        # Both conditions met: consecutive_losses >= 3 AND pnl > daily_loss_cap
        action = b.check(pnl=-4000, consecutive_losses=5)
        assert action == Action.DIAGNOSE  # Diagnosing the streak takes priority

    def test_custom_thresholds(self):
        b = CircuitBreaker(session="day", daily_loss_cap=10000, max_consecutive=5)
        assert b.check(pnl=0, consecutive_losses=4) == Action.CONTINUE
        assert b.check(pnl=0, consecutive_losses=5) == Action.DIAGNOSE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
