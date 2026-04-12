#!/usr/bin/env python3
"""Tests for Phase 2: Diagnostic Engine and Post-Session Review."""
import os
import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.diagnostic_engine import (
    diagnose_losing_streak,
    DiagnosticAction,
    TradeDiagnosis,
)
from scripts.daily_review import compute_session_metrics, _generate_recommendation


# ─── Diagnostic Engine ───────────────────────────────────────────────────

class TestDiagnosticEngine:
    def _make_trade(self, exit_reason="STOP_LOSS", momentum=20, vwap_dist=100, atr=50,
                    pnl_pts=-60, regime="trending", session="day"):
        return TradeDiagnosis(
            exit_reason=exit_reason,
            pnl_pts=pnl_pts,
            entry_diag={
                "momentum": momentum,
                "mom_velo": 5,
                "vwap_distance_pts": vwap_dist,
                "atr": atr,
                "squeeze_on_recent": False,
                "score": 15,
                "regime": regime,
                "session": session,
                "stop_loss_pts": 60,
            },
            session=session,
        )

    # Pattern 1: All stopped out, high VWAP distance
    def test_tighten_confirm_bars_on_vwap_chasing(self):
        trades = [
            self._make_trade(exit_reason="STOP_LOSS", momentum=40, vwap_dist=150, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=35, vwap_dist=120, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=45, vwap_dist=130, atr=50),
        ]
        action = diagnose_losing_streak(trades)
        assert action.action_type == "TIGHTEN_ENTRY"
        assert action.param == "confirm_bars"
        assert "VWAP" in action.reason

    # Pattern 1b: All stopped out, low momentum
    def test_tighten_momentum_on_weak_signals(self):
        trades = [
            self._make_trade(exit_reason="STOP_LOSS", momentum=15, vwap_dist=30, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=20, vwap_dist=25, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=10, vwap_dist=35, atr=50),
        ]
        action = diagnose_losing_streak(trades)
        assert action.action_type == "TIGHTEN_ENTRY"
        assert action.param == "min_momentum"
        assert "momentum" in action.reason.lower()

    # Pattern 2: All VWAP exits
    def test_vwap_exits_raise_momentum(self):
        trades = [
            self._make_trade(exit_reason="VWAP", momentum=25, vwap_dist=40),
            self._make_trade(exit_reason="VWAP", momentum=20, vwap_dist=35),
            self._make_trade(exit_reason="VWAP", momentum=30, vwap_dist=45),
        ]
        action = diagnose_losing_streak(trades)
        assert action.action_type == "TIGHTEN_ENTRY"
        assert action.param == "min_momentum"

    # Pattern 3: SHOCK regime
    def test_shock_regime_halts(self):
        trades = [
            self._make_trade(exit_reason="STOP_LOSS", momentum=50, vwap_dist=60, regime="SHOCK"),
            self._make_trade(exit_reason="STOP_LOSS", momentum=45, vwap_dist=55, regime="SHOCK"),
            self._make_trade(exit_reason="STOP_LOSS", momentum=55, vwap_dist=65, regime="SHOCK"),
        ]
        action = diagnose_losing_streak(trades)
        assert action.action_type == "HALT"
        assert "SHOCK" in action.reason

    # Pattern 4: Mixed exits, < 5 trades
    def test_mixed_exits_cooldown_small_sample(self):
        trades = [
            self._make_trade(exit_reason="STOP_LOSS"),
            self._make_trade(exit_reason="VWAP"),
            self._make_trade(exit_reason="ATR_TRAIL"),
        ]
        action = diagnose_losing_streak(trades)
        assert action.action_type == "COOLDOWN"
        assert action.cooldown_mins == 15

    # Pattern 5: 5+ consecutive losses — counter_vwap is best in day so SWITCH won't fire.
    # Instead, verify the engine reaches the Pattern 5 code path and returns CONTINUE
    # when current_strategy is already the best one.
    def test_five_losses_current_is_best_returns_continue(self):
        trades = [
            self._make_trade(exit_reason="STOP_LOSS", momentum=35, vwap_dist=30, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=30, vwap_dist=25, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=40, vwap_dist=35, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=38, vwap_dist=28, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=32, vwap_dist=32, atr=50),
        ]
        action = diagnose_losing_streak(trades, current_strategy="counter_vwap")
        # counter_vwap (PF=2.1) is the best day strategy → no switch available → CONTINUE
        assert action.action_type == "CONTINUE"
        assert "5" in action.reason  # Mentions the loss count

    def test_empty_trades_returns_continue(self):
        action = diagnose_losing_streak([])
        assert action.action_type == "CONTINUE"

    def test_single_trade_cooldown(self):
        # Use mixed exit to avoid STOP_LOSS pattern
        trades = [self._make_trade(exit_reason="ATR_TRAIL")]
        action = diagnose_losing_streak(trades)
        assert action.action_type == "COOLDOWN"  # < 5 trades

    def test_tighten_confirm_bars_delta(self):
        trades = [
            self._make_trade(exit_reason="STOP_LOSS", vwap_dist=150, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", vwap_dist=120, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", vwap_dist=130, atr=50),
        ]
        action = diagnose_losing_streak(trades)
        assert action.delta == 3  # Increase confirm_bars by 3

    def test_momentum_threshold_is_30(self):
        """Momentum < 30 triggers TIGHTEN_ENTRY"""
        trades_weak = [
            self._make_trade(exit_reason="STOP_LOSS", momentum=25, vwap_dist=30, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=20, vwap_dist=25, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=28, vwap_dist=35, atr=50),
        ]
        action = diagnose_losing_streak(trades_weak)
        assert action.action_type == "TIGHTEN_ENTRY"

    def test_vwap_distance_threshold_is_2x_atr(self):
        """VWAP distance > 2x ATR triggers confirm_bars tighten"""
        trades_chasing = [
            self._make_trade(exit_reason="STOP_LOSS", momentum=40, vwap_dist=110, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=35, vwap_dist=120, atr=50),
            self._make_trade(exit_reason="STOP_LOSS", momentum=45, vwap_dist=105, atr=50),
        ]
        action = diagnose_losing_streak(trades_chasing)
        assert action.param == "confirm_bars"

    def test_default_action_has_reason(self):
        trades = [self._make_trade(exit_reason="UNKNOWN")]
        action = diagnose_losing_streak(trades)
        assert action.reason  # Always has explanation


# ─── Post-Session Review Helpers ────────────────────────────────────────

class TestSessionMetrics:
    def test_compute_session_metrics_basic(self):
        trades = [
            {"pnl_pts": "100", "pnl_cash": "5000"},
            {"pnl_pts": "-50", "pnl_cash": "-2500"},
            {"pnl_pts": "200", "pnl_cash": "10000"},
        ]
        metrics = compute_session_metrics(trades)
        assert metrics["trade_count"] == 3
        assert metrics["wins"] == 2
        assert metrics["losses"] == 1
        assert metrics["win_rate"] == pytest.approx(66.67, rel=0.1)

    def test_compute_all_wins(self):
        trades = [
            {"pnl_pts": "100", "pnl_cash": "5000"},
            {"pnl_pts": "50", "pnl_cash": "2500"},
        ]
        metrics = compute_session_metrics(trades)
        assert metrics["pf"] > 100  # Very high (no losses → gross_loss=1 fallback)
        assert metrics["win_rate"] == 100.0

    def test_compute_all_losses(self):
        trades = [
            {"pnl_pts": "-100", "pnl_cash": "-5000"},
            {"pnl_pts": "-50", "pnl_cash": "-2500"},
        ]
        metrics = compute_session_metrics(trades)
        assert metrics["pf"] < 0.01  # Very low (no wins → gross_profit=1 fallback)
        assert metrics["win_rate"] == 0.0

    def test_compute_empty_trades(self):
        metrics = compute_session_metrics([])
        assert metrics["trade_count"] == 0
        assert metrics["pf"] == 0


class TestRecommendation:
    def test_no_issues(self):
        rec = _generate_recommendation(
            {"trade_count": 5, "pf": 2.0, "pnl_cash": 10000, "pnl_pts": 200, "wins": 3, "losses": 2, "win_rate": 60},
            None,
            [("counter_vwap", 2.1), ("spring_upthrust", 1.6)],
        )
        # No issues → recommendation shows ranking
        assert "counter_vwap" in rec

    def test_low_pf_warning(self):
        rec = _generate_recommendation(
            {"trade_count": 3, "pf": 0.8, "pnl_cash": -5000, "pnl_pts": -100, "wins": 1, "losses": 2, "win_rate": 33},
            None,
            [],
        )
        assert "PF" in rec and "1.0" in rec

    def test_diagnostic_tighten_entry(self):
        rec = _generate_recommendation(
            {"trade_count": 3, "pf": 0.5, "pnl_cash": -3000, "pnl_pts": -60, "wins": 0, "losses": 3, "win_rate": 0},
            DiagnosticAction("TIGHTEN_ENTRY", "VWAP chasing", "confirm_bars", 3),
            [],
        )
        assert "confirm_bars" in rec

    def test_diagnostic_halt(self):
        rec = _generate_recommendation(
            {"trade_count": 3, "pf": 0.3, "pnl_cash": -8000, "pnl_pts": -160, "wins": 0, "losses": 3, "win_rate": 0},
            DiagnosticAction("HALT", "SHOCK regime"),
            [],
        )
        assert "HALT" in rec

    def test_no_trades_warning(self):
        rec = _generate_recommendation(
            {"trade_count": 0, "pf": 0, "pnl_cash": 0, "pnl_pts": 0, "wins": 0, "losses": 0, "win_rate": 0},
            None,
            [],
        )
        assert "No trades" in rec or "too strict" in rec


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
