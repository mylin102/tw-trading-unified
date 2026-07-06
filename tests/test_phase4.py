#!/usr/bin/env python3
"""Tests for Phase 4: Session Config, Daily Review, Weekly Report."""
import os
import sys
import pytest
import yaml
import json
from pathlib import Path
from datetime import datetime, timedelta

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.session_config import SessionConfig, _get_nested, _set_nested
from scripts.weekly_report import compute_weekly_metrics, _generate_recommendations


# ─── Session Config ─────────────────────────────────────────────────────

class TestSessionConfig:
    def test_get_nested(self):
        d = {"a": {"b": {"c": 42}}}
        assert _get_nested(d, "a.b.c") == 42
        assert _get_nested(d, "a.b.x") is None

    def test_set_nested(self):
        d = {"a": {"b": 1}}
        _set_nested(d, "a.b", 2)
        assert d["a"]["b"] == 2
        _set_nested(d, "x.y.z", 3)
        assert d["x"]["y"]["z"] == 3

    def test_load_day_config(self):
        cfg = SessionConfig.load("day")
        assert cfg.get("active_strategy") == "tmf_spread"
        assert cfg.get("risk_mgmt.stop_loss_pts") == 60

    def test_load_night_config(self):
        cfg = SessionConfig.load("night")
        assert cfg.get("risk_mgmt.stop_loss_pts") == 80  # Wider stops for night

    def test_get_and_set(self, tmp_path):
        # Create temp config
        f = tmp_path / "futures_day.yaml"
        f.write_text("active_strategy: test\nrisk_mgmt:\n  stop_loss_pts: 60\n")

        cfg = SessionConfig.load("day")
        # Override file path for testing
        cfg._file = f
        assert cfg.get("risk_mgmt.stop_loss_pts") == 60
        cfg.set("risk_mgmt.stop_loss_pts", 70)
        cfg.save(backup=False)

        with open(f) as fh:
            data = yaml.safe_load(fh)
        assert data["risk_mgmt"]["stop_loss_pts"] == 70

    def test_atomic_save_creates_backup(self, tmp_path):
        f = tmp_path / "futures_day.yaml"
        f.write_text("active_strategy: test\n")

        cfg = SessionConfig("day", {"active_strategy": "updated"})
        cfg._file = f
        cfg.save(backup=True)

        # Check backup exists
        backups = list(tmp_path.glob("*.yaml.bak.*"))
        assert len(backups) >= 1

    def test_fallback_to_main_config(self, tmp_path):
        # No session files exist, should fallback
        import core.session_config as sc
        orig_fallback = sc._FALLBACK
        sc._FALLBACK = tmp_path / "futures.yaml"
        (tmp_path / "futures.yaml").write_text("active_strategy: fallback\n")
        sc._SESSION_FILES["test"] = tmp_path / "nonexistent.yaml"

        cfg = SessionConfig.load("test")
        assert cfg.get("active_strategy") == "fallback"

        sc._FALLBACK = orig_fallback


# ─── Weekly Report Helpers ──────────────────────────────────────────────

class TestWeeklyReport:
    def test_compute_weekly_metrics_empty(self):
        metrics = compute_weekly_metrics([])
        assert metrics["total"]["trade_count"] == 0
        assert metrics["total"]["pnl_pts"] == 0

    def test_compute_weekly_metrics_with_trades(self):
        now = datetime.now()
        trades = [
            {"type": "EXIT", "pnl_pts": "100", "session": "day", "timestamp": (now - timedelta(days=1)).isoformat()},
            {"type": "EXIT", "pnl_pts": "-50", "session": "day", "timestamp": (now - timedelta(days=2)).isoformat()},
            {"type": "EXIT", "pnl_pts": "200", "session": "night", "timestamp": (now - timedelta(days=3)).isoformat()},
        ]
        metrics = compute_weekly_metrics(trades)
        assert metrics["day"]["trade_count"] == 2
        assert metrics["day"]["wins"] == 1
        assert metrics["night"]["trade_count"] == 1
        assert metrics["total"]["pnl_pts"] == 250

    def test_recommendations_good_performance(self):
        metrics = {
            "total": {"pf": 2.0, "pnl_pts": 5000},
            "day": {"pf": 2.5, "pnl_pts": 3000},
            "night": {"pf": 1.5, "pnl_pts": 2000},
        }
        pipeline = [{"name": "counter_vwap", "day_pf": 2.1, "night_pf": 1.4, "status": "✅ Active"}]
        recs = _generate_recommendations(metrics, pipeline)
        assert any("PF" in r and "優異" in r for r in recs)

    def test_recommendations_poor_performance(self):
        metrics = {
            "total": {"pf": 0.8, "pnl_pts": -3000},
            "day": {"pf": 1.2, "pnl_pts": -1000},
            "night": {"pf": 0.5, "pnl_pts": -2000},
        }
        pipeline = [{"name": "counter_vwap", "day_pf": 0.8, "night_pf": 0.5, "status": "🔴 Retired"}]
        recs = _generate_recommendations(metrics, pipeline)
        assert any("PF" in r and "1.0" in r for r in recs)


# ─── Integration: Session Config + Daily Review ─────────────────────────

class TestSessionConfigIntegration:
    def test_config_roundtrip(self, tmp_path):
        """Config can be loaded, modified, saved, and reloaded."""
        f = tmp_path / "futures_day.yaml"
        f.write_text("active_strategy: counter_vwap\nrisk_mgmt:\n  stop_loss_pts: 60\n")

        # Create SessionConfig directly from dict, not via load()
        cfg = SessionConfig("day", {"active_strategy": "counter_vwap", "risk_mgmt": {"stop_loss_pts": 60}})
        cfg._file = f
        cfg.set("risk_mgmt.stop_loss_pts", 70)
        cfg.save(backup=False)

        # Reload directly from the file we just wrote
        with open(f) as fh:
            data = yaml.safe_load(fh)
        cfg2 = SessionConfig("day", data)
        cfg2._file = f
        assert cfg2.get("risk_mgmt.stop_loss_pts") == 70


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
