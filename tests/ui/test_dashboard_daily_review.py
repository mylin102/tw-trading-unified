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
    fake_streamlit.calls = []

    class _Column:
        # 2026-10-21 Hermes Agent: support both current and legacy page renders.
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def metric(self, label, *args, **kwargs):
            fake_streamlit.calls.append(("metric", label))

        def caption(self, text, *args, **kwargs):
            fake_streamlit.calls.append(("caption", text))

        def dataframe(self, data, *args, **kwargs):
            fake_streamlit.calls.append(("dataframe", data))

        def write(self, *args, **kwargs):
            return None

        def markdown(self, *args, **kwargs):
            return None

        def info(self, *args, **kwargs):
            return None

    for name in (
        "set_page_config", "title", "caption", "info", "error", "subheader",
        "markdown", "write",
    ):
        setattr(fake_streamlit, name, lambda *args, **kwargs: None)
    fake_streamlit.columns = lambda count: [_Column() for _ in range(count)]

    def _selectbox(label, options, index=0, **kwargs):
        fake_streamlit.calls.append(("selectbox", label, list(options)))
        return options[index]

    fake_streamlit.selectbox = _selectbox
    fake_streamlit.dataframe = lambda data, *args, **kwargs: fake_streamlit.calls.append(
        ("dataframe", data)
    )
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
        # 2026-10-21 Hermes Agent: same trading day includes its next day session.
        {"trade_id": "day-2", "fill_type": "ENTRY", "timestamp": "2026-10-21T09:00:00", "session": "day"},
        {"trade_id": "day-2", "fill_type": "EXIT", "timestamp": "2026-10-21T09:05:00", "session": "day",
         "realized_pnl": 10, "gross_pnl": 12, "fee": 1, "tax": 1},
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
    assert report["summary"]["trade_count"] == 4
    assert report["summary"]["net_pnl"] == 93
    assert report["summary"]["win_rate"] == 1
    assert report["summary"]["friction_cost"] == 9
    assert report["summary"]["unitemized_cost_rows"] == 1
    assert report["summary"]["gross_pnl"] is None
    assert report["sessions"]["day"]["trade_count"] == 1
    assert report["sessions"]["day"]["net_pnl"] == 10
    assert report["sessions"]["day"]["friction_cost"] == 2
    assert report["sessions"]["night"]["trade_count"] == 3
    assert report["sessions"]["night"]["net_pnl"] == 83
    assert report["exit_reasons"]["policy_j_trigger_path"] == {"GIVEBACK": 1}
    assert report["exit_reasons"]["fill_type"] == {"EXIT": 3, "RELEASE": 1}

    holds = report["holding_time"]
    assert holds["count"] == 3
    assert holds["median_seconds"] == 120
    assert holds["p25_seconds"] == 82.5
    assert holds["p75_seconds"] == 210
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


def test_trade_review_renders_trading_day_selector_and_daily_summary(monkeypatch, tmp_path):
    import json

    fills = [
        {"trade_id": "night", "fill_type": "ENTRY", "timestamp": "2026-10-20T15:10:00", "session": "night"},
        {"trade_id": "night", "fill_type": "EXIT", "timestamp": "2026-10-20T15:11:00", "session": "night",
         "realized_pnl": 8, "gross_pnl": 10, "fee": 1, "tax": 1},
    ]
    (tmp_path / "mts_trade_fills.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in fills), encoding="utf-8"
    )
    (tmp_path / "mts_spread_events.jsonl").write_text("", encoding="utf-8")

    page = _load_page_helpers(monkeypatch, tmp_path)
    calls = page["st"].calls
    selector = next(call for call in calls if call[0] == "selectbox")
    summary_labels = {call[1] for call in calls if call[0] == "metric"}

    assert selector[1] == "交易日"
    assert selector[2] == ["2026-10-21"]
    assert {"淨損益", "交易筆數", "勝率", "摩擦成本合計", "毛利"} <= summary_labels
