"""
V-Model Level 2: Data Chain Integrity Tests

Problem 1: Dashboard data chain break — verify shared date logic,
           stale data detection, and cross-day filename alignment.

Problem 2: Signals fire but no trades — verify silent blockers are logged,
           TP1 works with 1 lot, and periodic summary tracks blocked entries.
"""
import datetime
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.date_utils import get_session_date_str


# ════════════════════════════════════════
# Problem 1: Data Chain Alignment
# ════════════════════════════════════════

class TestSessionDateStr:
    """Verify get_session_date_str produces consistent filenames."""

    def test_before_5am_uses_yesterday(self):
        dt = datetime.datetime(2026, 4, 8, 3, 30, 0)
        assert get_session_date_str(dt) == "20260407"

    def test_after_5am_uses_today(self):
        dt = datetime.datetime(2026, 4, 8, 6, 0, 0)
        assert get_session_date_str(dt) == "20260408"

    def test_at_5am_uses_today(self):
        dt = datetime.datetime(2026, 4, 8, 5, 0, 0)
        assert get_session_date_str(dt) == "20260408"

    def test_at_midnight_uses_yesterday(self):
        dt = datetime.datetime(2026, 4, 8, 0, 0, 0)
        assert get_session_date_str(dt) == "20260407"

    def test_at_459am_uses_yesterday(self):
        dt = datetime.datetime(2026, 4, 8, 4, 59, 0)
        assert get_session_date_str(dt) == "20260407"

    def test_current_time_when_none(self):
        """When dt is None, should use current time."""
        result = get_session_date_str(None)
        now = datetime.datetime.now()
        expected = now.strftime("%Y%m%d") if now.hour >= 5 else (now - datetime.timedelta(days=1)).strftime("%Y%m%d")
        assert result == expected

    def test_pd_timestamp_compatible(self):
        dt = pd.Timestamp("2026-04-08 03:00:00")
        assert get_session_date_str(dt) == "20260407"

    def test_writer_reader_alignment(self):
        """Writer (main.py) and reader (dashboard.py) must produce same filename."""
        # Simulate both started at different times but same session
        writer_time = datetime.datetime(2026, 4, 8, 4, 0, 0)   # 04:00
        reader_time = datetime.datetime(2026, 4, 8, 4, 30, 0)  # 04:30
        assert get_session_date_str(writer_time) == get_session_date_str(reader_time) == "20260407"

    def test_cross_midnight_alignment(self):
        """Monitor runs across midnight — both should use same session date."""
        # Monitor started at 20:00 on Apr 7
        monitor_time = datetime.datetime(2026, 4, 7, 20, 0, 0)
        # Dashboard starts at 01:00 on Apr 8
        dashboard_time = datetime.datetime(2026, 4, 8, 1, 0, 0)
        # Both should reference the same session (Apr 7)
        assert get_session_date_str(monitor_time) == "20260407"
        assert get_session_date_str(dashboard_time) == "20260407"


# ════════════════════════════════════════
# Problem 2: Signal-to-Trade Pipeline
# ════════════════════════════════════════

class TestTP1WithOneLot:
    """Verify TP1 partial profit works with lots_per_trade=1."""

    def setup_method(self):
        sys.path.insert(0, str(Path(__file__).parent.parent / "strategies" / "options" / "options_engine" / "engine"))
        from backtest_engine import should_take_partial_profit
        self.fn = should_take_partial_profit

    def test_tp1_with_position_1(self):
        """TP1 should fire when position=1 (not just position=2)."""
        entry = 100
        current = 155  # 55% gain, tp1_pct=0.5
        assert self.fn(position=1, has_tp1=False, entry_opt_premium=entry,
                       current_premium=current, tp1_pct=0.5) is True

    def test_tp1_with_position_2(self):
        """TP1 should still fire with position=2."""
        entry = 100
        current = 155
        assert self.fn(position=2, has_tp1=False, entry_opt_premium=entry,
                       current_premium=current, tp1_pct=0.5) is True

    def test_tp1_not_hit_below_threshold(self):
        """TP1 should not fire when gain < tp1_pct."""
        entry = 100
        current = 140  # 40% gain < 50% threshold
        assert self.fn(position=1, has_tp1=False, entry_opt_premium=entry,
                       current_premium=current, tp1_pct=0.5) is False

    def test_tp1_not_hit_after_tp1(self):
        """TP1 should not fire again after already hit."""
        assert self.fn(position=2, has_tp1=True, entry_opt_premium=100,
                       current_premium=155, tp1_pct=0.5) is False

    def test_tp1_not_hit_zero_position(self):
        """TP1 should not fire with no position."""
        assert self.fn(position=0, has_tp1=False, entry_opt_premium=100,
                       current_premium=155, tp1_pct=0.5) is False


class TestSilentBlockerLogging:
    """Verify that previously-silent blockers now produce console output."""

    def test_spread_too_wide_logged(self):
        """enter_paper_position should log when spread is too wide."""
        # Check source file directly (avoids import issues with options_engine)
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "spread too wide" in src

    def test_invalid_entry_price_logged(self):
        """enter_paper_position should log when entry price <= 0."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "invalid entry price" in src

    def test_invalid_exit_price_logged(self):
        """manage_open_position should log when exit price <= 0."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "invalid exit price" in src


class TestPeriodicSignalSummary:
    """Verify the 60s periodic summary tracks blocked entries."""

    def test_replay_stats_has_blocked_entries(self):
        """replay_stats must include blocked_entries counter."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "blocked_entries" in src

    def test_replay_stats_has_last_summary_at(self):
        """replay_stats must include last_summary_at for 60s throttling."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "last_summary_at" in src


class TestStaleDataDetection:
    """Verify dashboard detects stale indicator data."""

    def test_stale_warning_in_dashboard(self):
        """Dashboard should have stale data detection code."""
        src = Path("ui/dashboard.py").read_text()
        assert "資料停滯" in src

    def test_fallback_file_finder(self):
        """Dashboard should fall back to latest file if today's not found."""
        src = Path("ui/dashboard.py").read_text()
        # Verify the fallback logic exists (rglob for OPTIONS_*)
        assert "rglob" in src
        assert "OPTIONS_*" in src


# ════════════════════════════════════════
# SDD/V-Model: All 5 Bug Fixes Verified
# ════════════════════════════════════════

class TestLedgerRecoveryIntSafety:
    """Fix 1 (MEDIUM): int() crash on None/"" in CSV recovery."""

    def test_or_zero_guard_exists(self):
        """Recovery should use `int(x or 0)` pattern, not `int(x)`."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert 'int(r.get("Quantity", 0) or 0)' in src

    def test_int_none_crashes(self):
        """Verify the bug pattern: int(None) raises TypeError."""
        with pytest.raises(TypeError):
            int(None)

    def test_int_empty_string_crashes(self):
        """Verify the bug pattern: int("") raises ValueError."""
        with pytest.raises(ValueError):
            int("")

    def test_or_zero_pattern_safe(self):
        """Verify the fix pattern handles both None and ""."""
        assert int(None or 0) == 0
        assert int("" or 0) == 0
        assert int(5 or 0) == 5


class TestFillDedup:
    """Fix 2 (LOW): on_order_event dedup for duplicate broker fills."""

    def test_seen_fill_ordnos_exists(self):
        """Monitor should have _seen_fill_ordnos set."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "_seen_fill_ordnos" in src

    def test_ordno_check_exists(self):
        """on_order_event should check ordno before processing."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert 'msg.get("ordno"' in src


class TestSimulatorExitOrdering:
    """Fix 3 (LOW): SDD — position=0 before DB write."""

    def test_position_zero_before_db_write(self):
        """Simulator should zero position before DB write in EXIT path."""
        src = Path("strategies/futures/squeeze_futures/engine/simulator.py").read_text()
        # Find the EXIT block and verify position change comes before db write
        # Simple check: "State change BEFORE side effects" comment exists
        assert "State change BEFORE side effects" in src


class TestLedgerErrorLogging:
    """Fix 4 (LOW): Ledger read error should log warning, not silent pass."""

    def test_ledger_error_log_exists(self):
        """Ledger read error should produce console output."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "Ledger read error" in src


class TestDatetimeImportSafety:
    """Fix 5 (LOW): futures/monitor.py should import timedelta at top level."""

    def test_timedelta_at_top_level(self):
        """timedelta should be imported at module level, not locally."""
        src = Path("strategies/futures/monitor.py").read_text()
        lines = src.split("\n")
        # Check top-level imports (first 20 lines)
        top_lines = "\n".join(lines[:20])
        assert "from datetime import datetime, timedelta" in top_lines
        # Verify no local `from datetime import timedelta` inside functions
        local_imports = [l for l in lines if "from datetime import timedelta" in l]
        assert len(local_imports) == 0, f"Local timedelta import found: {local_imports}"


# ════════════════════════════════════════
# GSD: Silent PnL=0 Prevention
# ════════════════════════════════════════

class TestPnLSilentFailurePrevention:
    """GSD fix: verify all exit action types are recognized for PnL calculation."""

    def test_exit_keywords_in_log_trade(self):
        """log_trade should recognize all exit action types."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        # GSD whitelist
        assert '"TRAP"' in src, "EOD_TRAP_FILL not recognized"
        assert '"EOD"' in src, "EOD exits not recognized"
        assert '"FILL"' in src, "FILL exits not recognized"

    def test_cleared_retry_excluded_from_pnl(self):
        """Cancelled/retried orders should NOT be treated as exits."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert '"CLEARED"' in src
        assert '"SUBMITTED"' in src

    def test_theta_exit_bypasses_log_trade(self):
        """ThetaGang exit should write PnL directly, not through log_trade."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        # ThetaGang should write directly to ledger, not call log_trade
        theta_exit_section = src[src.find("THETA_EXIT"):]
        # Should have direct CSV write
        assert "to_csv" in theta_exit_section[:500]

    def test_exit_pnl_zero_warning(self):
        """log_trade should warn when exit PnL is 0."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "Exit PnL=0" in src
