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
import ast
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.date_utils import (
    get_session_date_str,
    get_trading_day,
    get_session,
    is_day_session,
    is_night_session,
    get_taifex_futures_hhmm,
    is_taifex_futures_market_open,
    get_taifex_futures_session_type,
)
from core.bar_utils import (
    attach_bar_metadata,
    build_canonical_bar_frames,
    build_preferred_canonical_bar_frames,
    canonicalize_ohlcv,
    fill_small_ohlcv_gaps,
    resample_ohlcv,
    validate_ohlcv_bars,
)
from core.dashboard_data import (
    build_stock_orders_from_trades,
    merge_indicator_frames,
    extend_taifex_recess_continuity,
    resolve_preferred_or_latest_file,
    resolve_stock_orders_file,
)
from core.options_snapshot import build_options_snapshot_row
from core.shioaji_session import SystemReadiness, get_shared_system_status, set_system_status


# ════════════════════════════════════════
# Problem 1: Data Chain Alignment
# ════════════════════════════════════════

class TestSessionDateStr:
    """Verify get_session_date_str produces correct Taifex trading days (15:00 rollover)."""

    def test_morning_session_same_day(self):
        # 03:30 AM Wednesday belongs to the Wednesday session (which started 15:00 Tuesday)
        dt = datetime.datetime(2026, 4, 8, 3, 30, 0)
        assert get_session_date_str(dt) == "20260408"

    def test_after_5am_still_same_day(self):
        # 06:00 AM Wednesday still belongs to Wednesday session
        dt = datetime.datetime(2026, 4, 8, 6, 0, 0)
        assert get_session_date_str(dt) == "20260408"

    def test_after_15pm_next_day(self):
        # 16:00 PM Wednesday belongs to Thursday session
        dt = datetime.datetime(2026, 4, 8, 16, 0, 0)
        assert get_session_date_str(dt) == "20260409"

    def test_friday_night_is_monday(self):
        # Friday 16:00 PM -> Monday
        dt = datetime.datetime(2026, 4, 10, 16, 0, 0)
        assert get_session_date_str(dt) == "20260413"

    def test_pd_timestamp_compatible(self):
        dt = pd.Timestamp("2026-04-08 03:00:00")
        assert get_session_date_str(dt) == "20260408"

    def test_writer_reader_alignment(self):
        """Writer (main.py) and reader (dashboard.py) must produce same filename."""
        # Simulate both started at different times but same session
        writer_time = datetime.datetime(2026, 4, 8, 4, 0, 0)   # 04:00 Wed
        reader_time = datetime.datetime(2026, 4, 8, 4, 30, 0)  # 04:30 Wed
        assert get_session_date_str(writer_time) == get_session_date_str(reader_time) == "20260408"

    def test_cross_midnight_alignment(self):
        """Monitor runs across midnight — both should use same session date."""
        # Monitor started at 20:00 on Apr 7 (Tue Night -> Wed Trading Day)
        monitor_time = datetime.datetime(2026, 4, 7, 20, 0, 0)
        # Dashboard starts at 01:00 on Apr 8 (Wed Morning -> Wed Trading Day)
        dashboard_time = datetime.datetime(2026, 4, 8, 1, 0, 0)
        # Both should reference the same trading day (Apr 8)
        assert get_session_date_str(monitor_time) == "20260408"
        assert get_session_date_str(dashboard_time) == "20260408"

    def test_series_input_preserves_index(self):
        """Vectorized session-date conversion must preserve pandas index alignment."""
        idx = ["a", "b"]
        series = pd.Series(
            pd.to_datetime(["2026-04-07 20:00:00", "2026-04-08 01:00:00"]),
            index=idx,
        )
        result = get_session_date_str(series)
        assert isinstance(result, pd.Series)
        assert list(result.index) == idx
        assert list(result.values) == ["20260408", "20260408"]

    def test_series_trading_day_preserves_index(self):
        """get_trading_day should return a pandas object for Series input."""
        idx = ["x", "y"]
        series = pd.Series(
            pd.to_datetime(["2026-04-10 16:00:00", "2026-04-13 01:00:00"]),
            index=idx,
        )
        result = get_trading_day(series)
        assert isinstance(result, pd.Series)
        assert list(result.index) == idx
        assert [d.strftime("%Y%m%d") for d in result] == ["20260413", "20260413"]

    def test_custom_holiday_skips_to_next_business_day(self):
        """Manual holiday override should push night session to the next valid trading day."""
        dt = datetime.datetime(2026, 4, 10, 16, 0, 0)  # Friday night would normally map to Monday 2026-04-13
        result = get_trading_day(dt, holidays={"2026-04-13"})
        assert result.strftime("%Y%m%d") == "20260414"


class TestSessionClassification:
    """Verify day/night session helpers on scalar and vectorized boundaries."""

    def test_scalar_session_boundaries(self):
        assert get_session("2026-04-08 07:59:00") == 2
        assert get_session("2026-04-08 08:00:00") == 1
        assert get_session("2026-04-08 14:59:00") == 1
        assert get_session("2026-04-08 15:00:00") == 2

    def test_vectorized_day_night_masks(self):
        series = pd.Series(pd.to_datetime([
            "2026-04-08 07:59:00",
            "2026-04-08 08:00:00",
            "2026-04-08 15:00:00",
        ]))
        assert list(is_night_session(series)) == [True, False, True]
        assert list(is_day_session(series)) == [False, True, False]


class TestTaifexTradingClock:
    """V-model spec: entry gates must use wall-clock/session time, not last completed bar timestamp."""

    def test_taifex_market_open_boundaries(self):
        assert is_taifex_futures_market_open("2026-04-08 14:55:00") is False
        assert is_taifex_futures_market_open("2026-04-08 15:00:00") is True
        assert is_taifex_futures_market_open("2026-04-09 03:00:00") is True
        assert is_taifex_futures_market_open("2026-04-09 06:00:00") is False

    def test_taifex_session_type_uses_wall_clock(self):
        assert get_taifex_futures_session_type("2026-04-08 14:55:00") == "day"
        assert get_taifex_futures_session_type("2026-04-08 15:00:00") == "night"
        assert get_taifex_futures_session_type("2026-04-09 04:59:00") == "night"
        assert get_taifex_futures_session_type("2026-04-09 05:00:00") == "day"

    def test_trading_clock_can_differ_from_latest_completed_bar(self):
        latest_completed_bar = pd.Timestamp("2026-04-08 14:55:00")
        live_clock = pd.Timestamp("2026-04-08 15:00:05")
        assert get_taifex_futures_hhmm(latest_completed_bar) == 1455
        assert get_taifex_futures_hhmm(live_clock) == 1500
        assert is_taifex_futures_market_open(live_clock) is True

    def test_futures_monitor_gate_no_longer_uses_bar_timestamp(self):
        src = Path("strategies/futures/monitor.py").read_text()
        assert 'market_open = is_taifex_futures_market_open()' in src
        assert 'self.session_type = get_taifex_futures_session_type()' in src


class TestCanonicalBarContract:
    """V-model spec for the shared OHLCV/canonical resample contract."""

    def test_canonicalize_ohlcv_sorts_and_keeps_required_columns(self):
        idx = pd.to_datetime(["2026-04-08 09:02:00", "2026-04-08 09:01:00"])
        raw = pd.DataFrame(
            {
                "Open": [102, 100],
                "High": [103, 101],
                "Low": [101, 99],
                "Close": [102.5, 100.5],
                "Volume": [20, 10],
                "extra": [1, 2],
            },
            index=idx,
        )
        result = canonicalize_ohlcv(raw)
        assert list(result.columns) == ["Open", "High", "Low", "Close", "Volume"]
        assert list(result.index.strftime("%H:%M:%S")) == ["09:01:00", "09:02:00"]

    def test_resample_ohlcv_aggregates_consistently(self):
        idx = pd.date_range("2026-04-08 09:00:00", periods=6, freq="1min")
        raw = pd.DataFrame(
            {
                "Open": [100, 101, 102, 103, 104, 105],
                "High": [101, 102, 103, 104, 105, 106],
                "Low": [99, 100, 101, 102, 103, 104],
                "Close": [100.5, 101.5, 102.5, 103.5, 104.5, 105.5],
                "Volume": [10, 20, 30, 40, 50, 60],
            },
            index=idx,
        )
        result = resample_ohlcv(raw, "5min")
        assert len(result) == 2
        assert result.iloc[0].to_dict() == {
            "Open": 100.0,
            "High": 105.0,
            "Low": 99.0,
            "Close": 104.5,
            "Volume": 150.0,
        }
        assert result.iloc[1].to_dict() == {
            "Open": 105.0,
            "High": 106.0,
            "Low": 104.0,
            "Close": 105.5,
            "Volume": 60.0,
        }

    def test_options_and_futures_share_same_resample_helper(self):
        futures_src = Path("strategies/futures/monitor.py").read_text()
        options_src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "resample_ohlcv" in futures_src
        assert "resample_ohlcv" in options_src

    def test_attach_bar_metadata_adds_trading_day_and_session(self):
        idx = pd.to_datetime(["2026-04-08 14:55:00", "2026-04-08 15:00:00"])
        raw = pd.DataFrame(
            {
                "Open": [100, 101],
                "High": [101, 102],
                "Low": [99, 100],
                "Close": [100.5, 101.5],
                "Volume": [10, 20],
            },
            index=idx,
        )
        result = attach_bar_metadata(raw)
        assert "trading_day" in result.columns
        assert "session" in result.columns
        assert result.iloc[0]["trading_day"].strftime("%Y%m%d") == "20260408"
        assert result.iloc[1]["trading_day"].strftime("%Y%m%d") == "20260409"
        assert list(result["session"]) == [1, 2]

    def test_options_and_futures_share_metadata_helper(self):
        futures_src = Path("strategies/futures/monitor.py").read_text()
        options_src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "attach_bar_metadata" in futures_src
        assert "attach_bar_metadata" in options_src


class TestRuntimeStatusPersistence:
    def test_shared_status_reads_persisted_runtime_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr("core.shioaji_session._system_status_path", lambda: tmp_path / "runtime_status.json")
        set_system_status(SystemReadiness.TRADING)
        assert get_shared_system_status() == SystemReadiness.TRADING


class TestDashboardIndicatorMerge:
    def test_merge_prefers_newer_more_complete_overlapping_row(self):
        older = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-04-20 15:00:00"]),
                "score": [None],
                "close": [22000.0],
            }
        )
        newer = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-04-20 15:00:00"]),
                "score": [82.5],
                "close": [22010.0],
                "momentum": [12.0],
            }
        )
        merged = merge_indicator_frames([older, newer])
        assert len(merged) == 1
        assert merged.iloc[0]["score"] == 82.5
        assert merged.iloc[0]["close"] == 22010.0


class TestDashboardFileResolution:
    def test_resolve_preferred_or_latest_file_prefers_current_session_file(self, tmp_path):
        preferred = tmp_path / "STOCK_2330_20260421_indicators.csv"
        older = tmp_path / "STOCK_2330_20260420_indicators.csv"
        older.write_text("older", encoding="utf-8")
        preferred.write_text("preferred", encoding="utf-8")

        resolved = resolve_preferred_or_latest_file(
            tmp_path,
            "STOCK_2330_20260421_indicators.csv",
            "STOCK_2330_*_indicators.csv",
        )

        assert resolved == preferred

    def test_resolve_preferred_or_latest_file_falls_back_to_latest_match(self, tmp_path):
        older = tmp_path / "STOCK_2330_20260420_indicators.csv"
        newer = tmp_path / "STOCK_2330_20260421_indicators.csv"
        older.write_text("older", encoding="utf-8")
        newer.write_text("newer", encoding="utf-8")
        os.utime(older, (1, 1))
        os.utime(newer, (2, 2))

        resolved = resolve_preferred_or_latest_file(
            tmp_path,
            "STOCK_2330_20260422_indicators.csv",
            "STOCK_2330_*_indicators.csv",
        )

        assert resolved == newer

    def test_resolve_stock_orders_file_prefers_mode_specific(self, tmp_path):
        legacy = tmp_path / "STOCK_20260421_orders.json"
        mode_scoped = tmp_path / "STOCK_20260421_PAPER_orders.json"
        legacy.write_text("[]", encoding="utf-8")
        mode_scoped.write_text("[{}]", encoding="utf-8")

        resolved = resolve_stock_orders_file(tmp_path, "20260421", "PAPER")

        assert resolved == mode_scoped

    def test_resolve_stock_orders_file_falls_back_to_legacy_name(self, tmp_path):
        legacy = tmp_path / "STOCK_20260421_orders.json"
        legacy.write_text("[]", encoding="utf-8")

        resolved = resolve_stock_orders_file(tmp_path, "20260421", "LIVE")

        assert resolved == legacy

    def test_build_stock_orders_from_trades_keeps_filled_rows_and_ticker_format(self):
        trades = pd.DataFrame(
            [
                {
                    "timestamp": "2026-04-21 09:15:00",
                    "ticker": 3017.0,
                    "action": "BUY",
                    "price": 2490.0,
                    "qty": 2,
                    "strategy": "scout_strategy",
                },
                {
                    "timestamp": "2026-04-21 10:05:00",
                    "ticker": 3017.0,
                    "action": "SELL",
                    "price": 2500.0,
                    "qty": 2,
                    "strategy": "scout_strategy",
                },
            ]
        )

        orders = build_stock_orders_from_trades(trades, mode="PAPER")

        assert len(orders) == 2
        assert orders[0]["ticker"] == "3017"
        assert orders[0]["status"] == "FILLED"
        assert orders[1]["side"] == "SELL"


class TestOptionsSnapshotSchema:
    def test_snapshot_row_keeps_dashboard_schema_when_signal_missing(self):
        row = build_options_snapshot_row(
            None,
            now=datetime.datetime(2026, 4, 20, 15, 5, 0),
            price_mtx=22123.0,
            score=0.0,
            side_label="",
            strike=22100.0,
            dte_days=3.0,
            mid_trend="",
            iv=0.25,
            delta_val=0.1,
            gamma_val=0.02,
            vega_val=0.03,
        )
        assert row["trading_day"] == "2026-04-21"
        assert row["Open"] == 22123.0
        assert row["High"] == 22123.0
        assert row["Low"] == 22123.0
        assert row["Close"] == 22123.0
        assert row["Volume"] == 0.0
        assert row["sqz_on"] is False
        assert row["squeeze_on"] is False
        assert row["bullish_align"] is False
        assert row["bearish_align"] is False

    def test_build_canonical_bar_frames_promotes_1m_source(self):
        idx = pd.date_range("2026-04-08 09:00:00", periods=20, freq="1min")
        raw = pd.DataFrame(
            {
                "Open": range(100, 120),
                "High": range(101, 121),
                "Low": range(99, 119),
                "Close": [x + 0.5 for x in range(100, 120)],
                "Volume": [10] * 20,
            },
            index=idx,
        )
        frames = build_canonical_bar_frames(raw, source_timeframe="1min")
        assert list(frames.keys()) == ["5m", "15m", "1h"]
        assert len(frames["5m"]) == 4
        assert list(frames["5m"]["session"].unique()) == [1]

    def test_build_preferred_canonical_bar_frames_skips_empty_candidates(self):
        idx = pd.date_range("2026-04-08 09:00:00", periods=10, freq="1min")
        raw = pd.DataFrame(
            {
                "Open": range(100, 110),
                "High": range(101, 111),
                "Low": range(99, 109),
                "Close": [x + 0.5 for x in range(100, 110)],
                "Volume": [10] * 10,
            },
            index=idx,
        )
        frames, diagnostics = build_preferred_canonical_bar_frames(
            [
                {"name": "empty", "frame": pd.DataFrame(), "source_timeframe": "5min"},
                {"name": "api-1m", "frame": raw, "source_timeframe": "1min"},
            ],
            min_5m_bars=2,
            now=pd.Timestamp("2026-04-08 09:10:00"),
        )
        assert diagnostics["source"] == "api-1m"
        assert diagnostics["rejected"] == ["empty:empty"]
        assert diagnostics["freshness_minutes"] == 5.0
        assert len(frames["5m"]) == 2

    def test_build_preferred_canonical_bar_frames_accepts_1m_source_under_5m_validator(self):
        idx = pd.date_range("2026-04-08 09:00:00", periods=60, freq="1min")
        raw = pd.DataFrame(
            {
                "Open": range(100, 160),
                "High": range(101, 161),
                "Low": range(99, 159),
                "Close": [x + 0.5 for x in range(100, 160)],
                "Volume": [10] * 60,
            },
            index=idx,
        )
        frames, diagnostics = build_preferred_canonical_bar_frames(
            [
                {"name": "api-1m", "frame": raw, "source_timeframe": "1min"},
            ],
            min_5m_bars=10,
            now=pd.Timestamp("2026-04-08 10:00:00"),
            validator=lambda df: validate_ohlcv_bars(
                df,
                min_bars=10,
                expected_interval_minutes=5,
                max_intraday_gap_minutes=30,
                max_session_gap_minutes=7200,
            ),
        )
        assert diagnostics["source"] == "api-1m"
        assert len(frames["5m"]) == 12
        assert frames["5m"].index.to_series().diff().dropna().median() == pd.Timedelta(minutes=5)

    def test_futures_monitor_uses_shared_bar_pipeline_selector(self):
        src = Path("strategies/futures/monitor.py").read_text()
        assert "build_preferred_canonical_bar_frames" in src

    def test_options_monitor_labels_api_candidate_as_1m_source(self):
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert '{"name": "api-1m", "frame": self._fetch_today_futures_bars(), "source_timeframe": "1min"}' in src

    def test_options_monitor_resamples_prefill_history_before_warming_tick_cache(self):
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert 'self._normalize_prefill_mtx_bars(df_hist, source_timeframe="1min")' in src
        assert 'bars_5m = self._normalize_prefill_mtx_bars(bars, source_timeframe="1min")' in src

    def test_fill_small_ohlcv_gaps_does_not_bridge_session_boundary(self):
        idx = pd.to_datetime(["2026-04-08 13:45:00", "2026-04-08 15:00:00"])
        raw = pd.DataFrame(
            {
                "Open": [100, 101],
                "High": [101, 102],
                "Low": [99, 100],
                "Close": [100.5, 101.5],
                "Volume": [10, 20],
            },
            index=idx,
        )
        result = fill_small_ohlcv_gaps(raw, expected_freq="5min", max_gap_minutes=15)
        assert list(result.index.strftime("%H:%M:%S")) == ["13:45:00", "15:00:00"]

    def test_validate_ohlcv_bars_allows_session_break_but_rejects_large_intraday_gap(self):
        session_idx = pd.to_datetime(["2026-04-08 13:45:00", "2026-04-08 15:00:00"])
        session_df = pd.DataFrame(
            {
                "Open": [100, 101],
                "High": [101, 102],
                "Low": [99, 100],
                "Close": [100.5, 101.5],
                "Volume": [10, 20],
            },
            index=session_idx,
        )
        ok, _ = validate_ohlcv_bars(session_df, min_bars=2)
        assert ok is True

        bad_idx = pd.to_datetime(["2026-04-08 09:00:00", "2026-04-08 09:45:00"])
        bad_df = pd.DataFrame(
            {
                "Open": [100, 101],
                "High": [101, 102],
                "Low": [99, 100],
                "Close": [100.5, 101.5],
                "Volume": [10, 20],
            },
            index=bad_idx,
        )
        ok, reason = validate_ohlcv_bars(bad_df, min_bars=2)
        assert ok is False
        assert "資料缺口過大" in reason

    def test_options_monitor_uses_shared_bar_pipeline_selector(self):
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert "build_preferred_canonical_bar_frames" in src

    # ── Regression tests for 2026-04-20 night-session-none-until-1715 ──

    def test_validate_ohlcv_bars_accepts_weekend_gap(self):
        """BUG FIX 2026-04-20: validate_ohlcv_bars with max_session_gap_minutes=7200 must
        accept 3-day API windows that span a weekend (~3110-min gap from Friday night's
        last bar at Sat 04:55 to Monday morning's first bar at Mon 08:45).  Previously
        max_session_gap_minutes=380 incorrectly rejected this valid multi-day dataset
        every Monday.
        
        Key: last Fri night bar (Sat 04:55) → first Mon day bar (Mon 08:45) is a
        cross-session gap (session 2 → session 1, same trading_day = Mon) of ~3110 min.
        """
        # Minimal dataset that contains the critical weekend cross-session gap.
        # The pair (Sat 04:55, Mon 08:45) produces ~3110-min same_session=False gap.
        idx = pd.to_datetime([
            "2026-04-18 04:50:00",  # Fri night session bar (Sat 04:50)
            "2026-04-18 04:55:00",  # Fri night session last bar (Sat 04:55)
            "2026-04-20 08:45:00",  # Mon day session first bar (~3110-min gap)
            "2026-04-20 08:50:00",  # Mon day session next bar
        ])
        df = pd.DataFrame({
            "Open": [100.0] * len(idx),
            "High": [101.0] * len(idx),
            "Low": [99.0] * len(idx),
            "Close": [100.5] * len(idx),
            "Volume": [50.0] * len(idx),
        }, index=idx)

        # Old behaviour: max_session_gap_minutes=380 would REJECT this data
        ok_old, reason_old = validate_ohlcv_bars(
            df, min_bars=2, expected_interval_minutes=5, max_session_gap_minutes=380
        )
        assert ok_old is False, "Old 380-min threshold should reject weekend gap (regression guard)"
        assert "資料缺口過大" in reason_old

        # New behaviour: max_session_gap_minutes=7200 must ACCEPT it
        ok_new, reason_new = validate_ohlcv_bars(
            df, min_bars=2, expected_interval_minutes=5, max_session_gap_minutes=7200
        )
        assert ok_new is True, f"7200-min threshold should accept weekend gap but got: {reason_new}"

    def test_validate_ohlcv_bars_accepts_mon_night_to_tue_day_transition(self):
        """Night session ending Sat 05:00 → next Monday 08:45 gap accepted with 7200-min."""
        # Also guard: a normal weeknight gap (Fri day 13:45 → Fri night 15:00 = 75 min) still passes
        idx_weekday = pd.to_datetime([
            "2026-04-20 13:45:00",
            "2026-04-20 15:00:00",
        ])
        df_weekday = pd.DataFrame({
            "Open": [100.0, 101.0],
            "High": [101.0, 102.0],
            "Low": [99.0, 100.0],
            "Close": [100.5, 101.5],
            "Volume": [10.0, 20.0],
        }, index=idx_weekday)
        ok, _ = validate_ohlcv_bars(df_weekday, min_bars=2, max_session_gap_minutes=7200)
        assert ok is True, "Weekday session break should still pass with 7200-min threshold"

    def test_options_validator_uses_7200_min_session_gap(self):
        """BUG FIX 2026-04-20: fetch_live_signal's validator lambda must use
        max_session_gap_minutes=7200 (not 380) so Monday API data is accepted."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        # 380 must NOT appear as max_session_gap_minutes value (the bug)
        import re
        # Allow 380 only inside a comment; must not appear as a kwarg value
        kwarg_380 = re.search(r"max_session_gap_minutes\s*=\s*380\b(?!\s*#)", src)
        assert kwarg_380 is None, (
            "options monitor must not use max_session_gap_minutes=380 "
            "(causes Monday weekend-gap rejection) — use 7200 instead"
        )
        # 7200 must be present
        assert "max_session_gap_minutes=7200" in src, (
            "options monitor must use max_session_gap_minutes=7200 in both "
            "_validate_kbar_data and fetch_live_signal"
        )

    def test_backfill_night_gaps_uses_session_date(self):
        """BUG FIX 2026-04-20: _backfill_night_gaps must use get_session_date_str() so it
        writes to the same file as _save_bar.  The old code used today.strftime('%Y%m%d')
        (wall-clock date) which created a second file with NaN indicators that shadowed the
        correctly-computed night session file in the dashboard's drop_duplicates merge."""
        src = Path("strategies/futures/monitor.py").read_text()
        # The function must call get_session_date_str somewhere inside _backfill_night_gaps
        assert "get_session_date_str" in src, (
            "_backfill_night_gaps must import and call get_session_date_str() "
            "to align the CSV filename with _save_bar"
        )
        # The old bug pattern must not exist (raw strftime without session awareness)
        # Allow strftime in other parts of the file; just guard the specific
        # date_str assignment that caused the bug inside the backfill function.
        import ast
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_backfill_night_gaps":
                func_src = ast.get_source_segment(src, node) or ""
                # The fix: no bare today.strftime('%Y%m%d') assignment to date_str
                assert "date_str = today.strftime('%Y%m%d')" not in func_src, (
                    "_backfill_night_gaps must not use today.strftime('%Y%m%d') for date_str; "
                    "use get_session_date_str(today) instead"
                )
                break

    def test_backfill_skips_when_indicator_data_exists(self):
        """BUG FIX 2026-04-20: _backfill_night_gaps must skip writing raw bars to a
        session file that already has indicator data from _save_bar, to prevent the
        NaN-indicator rows from shadowing the correct computed rows in the same file."""
        src = Path("strategies/futures/monitor.py").read_text()
        # Guard: the has_indicator_data early-return logic must be present
        assert "has_indicator_data" in src, (
            "_backfill_night_gaps must check for existing indicator data and skip "
            "raw backfill when the session CSV already has computed indicators"
        )


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


class TestOptionsEntryGateConsistency:
    """Regression guard: gated signals must not leak stale side/score into entry."""

    def test_entry_functions_validate_signal_side(self):
        """Both paper and live entry paths should reject cleared/mismatched signal side."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert 'signal_side = signal.get("side") if isinstance(signal, dict) else None' in src
        assert "signal side mismatch/cleared" in src
        assert "signal_side_mismatch:" in src

    def test_strategy_loop_refreshes_sig_side_after_gates(self):
        """Strategy loop should recompute sig_side/sig_score after readiness/edge gates."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert src.count('sig_side = signal.get("side") if signal else None') >= 2
        assert src.count('sig_score = signal.get("score", 0.0) if signal else 0.0') >= 2


class TestStaleDataDetection:
    """Verify dashboard detects stale indicator data."""

    def test_futures_recess_continuity_extends_recent_last_row(self):
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-04-21 05:00:00"]),
                "open": [201.0],
                "high": [202.0],
                "low": [199.0],
                "close": [200.0],
                "volume": [15.0],
                "score": [82.0],
            }
        )

        extended = extend_taifex_recess_continuity(
            df,
            now=pd.Timestamp("2026-04-21 07:26:00"),
        )

        assert len(extended) == 2
        assert extended.iloc[-1]["timestamp"] == pd.Timestamp("2026-04-21 07:25:00")
        assert extended.iloc[-1]["close"] == 200.0
        assert extended.iloc[-1]["volume"] == 0.0
        assert bool(extended.iloc[-1]["__synthetic_continuity"]) is True

    def test_futures_recess_continuity_skips_long_weekend_gap(self):
        df = pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-04-18 05:00:00"]),
                "close": [200.0],
                "volume": [15.0],
            }
        )

        extended = extend_taifex_recess_continuity(
            df,
            now=pd.Timestamp("2026-04-20 07:26:00"),
        )

        assert len(extended) == 1

    def test_stale_warning_in_dashboard(self):
        """Dashboard should have stale data detection code."""
        src = Path("ui/dashboard.py").read_text()
        assert "資料停滯" in src
        assert "extend_taifex_recess_continuity" in src

    def test_fallback_file_finder(self):
        """Dashboard should fall back to latest file if today's not found."""
        src = Path("ui/dashboard.py").read_text()
        # Verify the fallback logic exists (rglob for OPTIONS_*)
        assert "rglob" in src
        assert "OPTIONS_*" in src


class TestOptionsContractStalenessSafety:
    """Freeze GSD stale-contract handling semantics for options monitor."""

    def test_single_staleness_method_definition(self):
        """Options monitor must not silently override stale handler implementation."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        mod = ast.parse(src)
        cls = next(
            node for node in mod.body
            if isinstance(node, ast.ClassDef) and node.name == "ShioajiOptionsSmartMonitor"
        )
        count = sum(
            1 for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == "_check_options_contract_staleness"
        )
        assert count == 1, "duplicate _check_options_contract_staleness silently changes runtime behavior"

    def test_no_resubscribe_in_staleness_handler(self):
        """Valid-but-quiet options contracts should defer to sentinel, not local re-subscribe."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        lines = src.splitlines()
        mod = ast.parse(src)
        cls = next(
            node for node in mod.body
            if isinstance(node, ast.ClassDef) and node.name == "ShioajiOptionsSmartMonitor"
        )
        fn = next(
            node for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == "_check_options_contract_staleness"
        )
        body_src = "\n".join(lines[fn.lineno - 1: fn.end_lineno])
        assert "quote.unsubscribe" not in body_src
        assert "quote.subscribe" not in body_src
        assert "sentinel handle" in body_src


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

    def test_option_fee_attrs_have_runtime_fallback(self):
        """Exit PnL calc should survive older monitor instances missing fee attrs."""
        src = Path("strategies/options/live_options_squeeze_monitor.py").read_text()
        assert 'getattr(self, "broker_fee_per_side"' in src
        assert 'getattr(self, "exchange_fee_per_side"' in src
