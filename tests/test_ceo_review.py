#!/usr/bin/env python3
"""Tests for CEO Review CLI tool."""
import os
import sys
import json
import pytest
import pandas as pd
from pathlib import Path
from unittest.mock import patch, MagicMock
from dataclasses import asdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.tools.ceo_review import (
    load_config,
    load_backtest_results,
    load_trade_history,
    check_strategy_scope,
    check_risk_reward,
    check_capital_efficiency,
    check_live_readiness,
    generate_verdict,
    ReviewFinding,
    ReviewReport,
    DEFAULT_MIN_PF,
    DEFAULT_MAX_DD,
    DEFAULT_MIN_WR,
    DEFAULT_MIN_TRADES,
)


class TestReviewFinding:
    def test_finding_creation(self):
        f = ReviewFinding(
            category="TEST",
            status="PASS",
            metric="Test Metric",
            value="100",
            threshold=">= 50",
            recommendation="",
        )
        assert f.category == "TEST"
        assert f.status == "PASS"
        assert f.metric == "Test Metric"
        assert f.value == "100"
        assert f.threshold == ">= 50"


class TestReviewReport:
    def test_report_creation(self):
        report = ReviewReport(
            review_id="test_001",
            timestamp="2026-04-12T10:00:00",
            scope="futures",
            findings=[],
            proposals=["proposal1"],
            accepted=["proposal1"],
            deferred=[],
            verdict="✅ CLEARED",
            summary="All checks passed",
        )
        assert report.review_id == "test_001"
        assert report.scope == "futures"
        assert report.verdict == "✅ CLEARED"
        assert len(report.proposals) == 1


class TestLoadConfig:
    def test_load_existing_config(self):
        cfg = load_config(Path("config/futures.yaml"))
        assert isinstance(cfg, dict)
        assert "strategy" in cfg or len(cfg) == 0  # might be empty if file missing
        if "strategy" in cfg:
            assert cfg.get("active_strategy") == cfg["strategy"].get("active_strategy")

    def test_load_nonexistent_config(self):
        cfg = load_config(Path("config/nonexistent.yaml"))
        assert cfg == {}


class TestCheckStrategyScope:
    def test_valid_scope(self):
        futures_cfg = {
            "strategy": {
                "active_strategy": "vol_squeeze",
                "auto_select": True,
                "counter_mode": {"enabled": True},
            },
        }
        options_cfg = {"active_mode": "V2"}
        findings = check_strategy_scope(futures_cfg, options_cfg)
        assert len(findings) == 4
        # Active strategy should pass
        active_finding = next(f for f in findings if f.metric == "Active Strategy")
        assert active_finding.status == "PASS"
        # Auto select should pass
        auto_finding = next(f for f in findings if f.metric == "Auto Select")
        assert auto_finding.status == "PASS"
        # Options V2 should pass
        options_finding = next(f for f in findings if f.metric == "Options Mode")
        assert options_finding.status == "PASS"

    def test_missing_active_strategy(self):
        futures_cfg = {"strategy": {"active_strategy": "", "auto_select": False, "counter_mode": {"enabled": False}}}
        options_cfg = {"active_mode": "V1"}
        findings = check_strategy_scope(futures_cfg, options_cfg)
        active_finding = next(f for f in findings if f.metric == "Active Strategy")
        assert active_finding.status == "FAIL" or active_finding.status == "WARN"
        options_finding = next(f for f in findings if f.metric == "Options Mode")
        assert options_finding.status == "FAIL"

    def test_empty_configs(self):
        findings = check_strategy_scope({}, {})
        assert len(findings) == 4
        # Most should fail/warn with empty configs
        statuses = [f.status for f in findings]
        assert "FAIL" in statuses or "WARN" in statuses


class TestCheckRiskReward:
    def test_passing_backtest_data(self):
        df = pd.DataFrame({
            "PF": [1.95, 1.5, 1.2],
            "PnL": [32285, 20000, 5000],
            "MaxDD%": [-7.2, -10.0, -12.0],
            "Win%": [40.7, 35.0, 32.0],
            "Trades": [86, 50, 30],
            "sharpe": [2.5, 1.8, 1.2],
        })
        findings = check_risk_reward(df, min_pf=1.3, max_dd=-15.0, min_wr=30.0, min_trades=10)
        # Should have findings for each metric
        assert len(findings) >= 5
        # Best row should pass PF
        pf_finding = next(f for f in findings if f.metric == "Profit Factor")
        assert pf_finding.status == "PASS"
        # PnL should pass
        pnl_finding = next(f for f in findings if f.metric == "Net PnL (TWD)")
        assert pnl_finding.status == "PASS"

    def test_failing_backtest_data(self):
        df = pd.DataFrame({
            "PF": [0.8],
            "PnL": [-5000],
            "MaxDD%": [-20.0],
            "Win%": [25.0],
            "Trades": [5],
        })
        findings = check_risk_reward(df, min_pf=1.3, max_dd=-15.0, min_wr=30.0, min_trades=10)
        pf_finding = next(f for f in findings if f.metric == "Profit Factor")
        assert pf_finding.status == "FAIL"
        pnl_finding = next(f for f in findings if f.metric == "Net PnL (TWD)")
        assert pnl_finding.status == "FAIL"
        dd_finding = next(f for f in findings if f.metric == "Max Drawdown %")
        assert dd_finding.status == "FAIL"

    def test_empty_backtest_data(self):
        df = pd.DataFrame()
        findings = check_risk_reward(df, min_pf=1.3, max_dd=-15.0, min_wr=30.0, min_trades=10)
        assert len(findings) == 1
        assert findings[0].status == "FAIL"
        assert findings[0].metric == "Backtest Data"

    def test_mixed_results(self):
        df = pd.DataFrame({
            "PF": [1.5],
            "PnL": [10000],
            "MaxDD%": [-10.0],
            "Win%": [28.0],  # Below threshold
            "Trades": [50],
        })
        findings = check_risk_reward(df, min_pf=1.3, max_dd=-15.0, min_wr=30.0, min_trades=10)
        wr_finding = next(f for f in findings if f.metric == "Win Rate %")
        assert wr_finding.status == "WARN"  # Below min_wr but not fail


class TestCheckCapitalEfficiency:
    def test_conservative_settings(self):
        futures_cfg = {
            "trade_mgmt": {"lots_per_trade": 1, "max_positions": 1},
            "execution": {"initial_balance": 100000},
        }
        options_cfg = {
            "risk_mgmt": {"lots_per_trade": 1, "max_positions": 1, "initial_capital": 40000, "max_daily_loss": 0.02},
            "theta_gang": {"enabled": True, "max_loss_pct": 0.5},
        }
        findings = check_capital_efficiency(futures_cfg, options_cfg, pd.DataFrame())
        # Lots should pass
        lots_finding = next(f for f in findings if f.metric == "Futures Lots")
        assert lots_finding.status == "PASS"
        # Max positions should pass
        max_pos_finding = next(f for f in findings if f.metric == "Futures Max Positions")
        assert max_pos_finding.status == "PASS"
        # Daily loss should pass
        daily_loss_finding = next(f for f in findings if f.metric == "Max Daily Loss %")
        assert daily_loss_finding.status == "PASS"

    def test_aggressive_settings(self):
        futures_cfg = {
            "trade_mgmt": {"lots_per_trade": 5, "max_positions": 5},
            "execution": {"initial_balance": 100000},
        }
        options_cfg = {
            "risk_mgmt": {"lots_per_trade": 3, "max_positions": 5, "initial_capital": 40000, "max_daily_loss": 0.10},
            "theta_gang": {"enabled": True, "max_loss_pct": 2.0},
        }
        findings = check_capital_efficiency(futures_cfg, options_cfg, pd.DataFrame())
        lots_finding = next(f for f in findings if f.metric == "Futures Lots")
        assert lots_finding.status == "WARN"
        max_pos_finding = next(f for f in findings if f.metric == "Futures Max Positions")
        assert max_pos_finding.status == "FAIL"
        daily_loss_finding = next(f for f in findings if f.metric == "Max Daily Loss %")
        assert daily_loss_finding.status == "FAIL"


class TestCheckLiveReadiness:
    def test_all_pass(self):
        findings = [
            ReviewFinding("CAT", "PASS", "m1", "1", ">= 1"),
            ReviewFinding("CAT", "PASS", "m2", "2", ">= 2"),
            ReviewFinding("CAT", "PASS", "m3", "3", ">= 3"),
        ]
        futures_cfg = {"live_trading": False}
        options_cfg = {"live_trading": False}
        readiness = check_live_readiness(futures_cfg, options_cfg, findings)
        assert len(readiness) == 5
        # No failures
        fail_finding = next(f for f in readiness if f.metric == "Critical Failures")
        assert fail_finding.status == "PASS"
        # Pass rate should be 100%
        rate_finding = next(f for f in readiness if f.metric == "Pass Rate")
        assert rate_finding.status == "PASS"

    def test_with_failures(self):
        findings = [
            ReviewFinding("CAT", "FAIL", "m1", "0", ">= 1"),
            ReviewFinding("CAT", "PASS", "m2", "2", ">= 2"),
        ]
        futures_cfg = {"live_trading": True}
        options_cfg = {"live_trading": False}
        readiness = check_live_readiness(futures_cfg, options_cfg, findings)
        fail_finding = next(f for f in readiness if f.metric == "Critical Failures")
        assert fail_finding.status == "FAIL"
        # Live mode warning
        live_finding = next(f for f in readiness if f.metric == "Futures Live Mode")
        assert live_finding.status == "WARN"


class TestGenerateVerdict:
    def test_rejected(self):
        findings = [
            ReviewFinding("CAT", "FAIL", "m1", "0", ">= 1"),
            ReviewFinding("CAT", "PASS", "m2", "2", ">= 2"),
        ]
        verdict, summary = generate_verdict(findings)
        assert "REJECTED" in verdict
        assert "critical failure" in summary.lower()

    def test_cleared(self):
        findings = [
            ReviewFinding("CAT", "PASS", "m1", "1", ">= 1"),
            ReviewFinding("CAT", "PASS", "m2", "2", ">= 2"),
            ReviewFinding("CAT", "PASS", "m3", "3", ">= 3"),
        ]
        verdict, summary = generate_verdict(findings)
        assert "CLEARED" in verdict
        assert "passed" in summary.lower()

    def test_conditional(self):
        findings = [
            ReviewFinding("CAT", "PASS", f"m{i}", str(i), f">= {i}")
            for i in range(6)
        ]
        # Add 4 warnings
        for i in range(4):
            findings.append(ReviewFinding("CAT", "WARN", f"w{i}", str(i), f">= {i}"))
        verdict, summary = generate_verdict(findings)
        assert "CONDITIONAL" in verdict
        assert "warning" in summary.lower()


class TestReviewReportSerialization:
    def test_save_and_load(self, tmp_path):
        import json
        report = ReviewReport(
            review_id="test_serialization",
            timestamp="2026-04-12T10:00:00",
            scope="all",
            findings=[
                {"category": "TEST", "status": "PASS", "metric": "m1", "value": "1", "threshold": ">= 1"}
            ],
            proposals=["p1"],
            accepted=["p1"],
            deferred=[],
            verdict="✅ CLEARED",
            summary="Test passed",
        )
        # Manually save
        filepath = tmp_path / "test_report.json"
        with open(filepath, "w") as f:
            json.dump(asdict(report), f, default=str)

        with open(filepath) as f:
            loaded = json.load(f)
        assert loaded["review_id"] == "test_serialization"
        assert loaded["scope"] == "all"
        assert loaded["verdict"] == "✅ CLEARED"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
