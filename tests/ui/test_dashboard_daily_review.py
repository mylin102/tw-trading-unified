# 2026-10-21 Hermes Agent: cover daily Trade Review summaries from synthetic fills.
import runpy
import sys
import types
from pathlib import Path


def _load_page_helpers(monkeypatch, tmp_path):
    import core.runtime_paths as runtime_paths

    monkeypatch.setattr(
        runtime_paths,
        "mts_runtime_path",
        lambda *parts: str(tmp_path.joinpath(*parts[-1:])),
    )
    fake_streamlit = types.ModuleType("streamlit")
    for name in ("set_page_config", "title", "caption", "info", "error"):
        setattr(fake_streamlit, name, lambda *args, **kwargs: None)
    monkeypatch.setitem(sys.modules, "streamlit", fake_streamlit)
    page = Path(__file__).parents[2] / "ui" / "pages" / "03_trade_review.py"
    return runpy.run_path(str(page))


def test_daily_review_groups_trading_days_costs_sessions_reasons_and_holding(
    monkeypatch, tmp_path
):
    page = _load_page_helpers(monkeypatch, tmp_path)
    fills = [
        {"trade_id": "day-1", "fill_type": "ENTRY", "timestamp": "2026-10-20T14:50:00", "session": "day"},
        {"trade_id": "day-1", "fill_type": "EXIT", "timestamp": "2026-10-20T14:55:00", "session": "day",
         "realized_pnl": 80, "gross_pnl": 100, "fee": 10, "tax": 10},
        {"trade_id": "night-1", "fill_type": "ENTRY", "timestamp": "2026-10-20T15:10:00", "session": "night"},
        {"trade_id": "night-1", "fill_type": "COMBINED_EXIT", "timestamp": "2026-10-20T15:12:00", "session": "night",
         "realized_pnl": 46, "gross_pnl": 50, "fee": 2, "tax": 2},
        {"trade_id": "night-2", "fill_type": "ENTRY", "timestamp": "2026-10-20T16:00:00", "session": "night"},
        {"trade_id": "night-2", "fill_type": "RELEASE", "timestamp": "2026-10-20T16:00:30", "session": "night",
         "realized_pnl": 22, "gross_pnl": 25, "fee": 2, "tax": 1},
        # 2026-10-21 Hermes Agent: RELEASE is partial; EXIT closes this trade_id.
        {"trade_id": "night-2", "fill_type": "EXIT", "timestamp": "2026-10-20T16:00:45", "session": "night",
         "realized_pnl": 0, "gross_pnl": 0, "fee": 0, "tax": 0},
        # Legacy closed trade has no entry fill or itemized cost fields.
        {"trade_id": "legacy-1", "fill_type": "EXIT", "timestamp": "2026-10-20T16:05:00", "session": "night",
         "realized_pnl": 15},
    ]
    events = [
        {"trade_id": "night-1", "event": "COMBINED_EXIT_SUBMITTED",
         "ts": "2026-10-20T15:12:00", "policy_j_trigger_path": "GIVEBACK"}
    ]

    report = page["build_daily_review"](
        fills, events, today_trading_day="2026-10-21"
    )

    assert report["available_days"] == ["2026-10-21", "2026-10-20"]
    assert report["selected_day"] == "2026-10-21"
    assert report["summary"]["trade_count"] == 3
    assert report["summary"]["net_pnl"] == 83
    assert report["summary"]["friction_cost"] == 7
    assert report["summary"]["unitemized_cost_rows"] == 1
    assert report["summary"]["gross_pnl"] is None
    assert report["sessions"]["day"]["trade_count"] == 0
    assert report["sessions"]["night"]["trade_count"] == 3
    assert report["sessions"]["night"]["net_pnl"] == 83
    assert report["exit_reasons"]["policy_j_trigger_path"] == {"GIVEBACK": 1}
    assert report["exit_reasons"]["fill_type"] == {"EXIT": 2, "RELEASE": 1}

    holds = report["holding_time"]
    assert holds["count"] == 2
    assert holds["median_seconds"] == 82.5
    assert holds["p25_seconds"] == 63.75
    assert holds["p75_seconds"] == 101.25
    assert holds["under_60_seconds"] == 1

    legacy = next(row for row in report["trades"] if row["trade_id"] == "legacy-1")
    assert legacy["entry_time"] == "進場時間缺失"
    assert legacy["holding_seconds"] is None
    assert legacy["friction_cost"] is None


def test_daily_review_defaults_to_today_when_ledger_has_today(monkeypatch, tmp_path):
    page = _load_page_helpers(monkeypatch, tmp_path)
    fills = [
        {"trade_id": "today", "fill_type": "ENTRY", "timestamp": "2026-10-20T15:01:00", "session": "night"},
        {"trade_id": "today", "fill_type": "EXIT", "timestamp": "2026-10-20T15:02:00", "session": "night",
         "realized_pnl": 1, "gross_pnl": 1, "fee": 0, "tax": 0},
    ]

    report = page["build_daily_review"](
        fills, [], today_trading_day="2026-10-21"
    )

    assert report["selected_day"] == "2026-10-21"
