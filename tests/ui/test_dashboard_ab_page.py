# 2026-10-20 Hermes Agent spec, implemented by Codex: verify the read-only paired A/B page contract.
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest


# 2026-10-20 Hermes Agent: verify the requested automatic-multipage filename and page title.
PAGE_PATH = Path(__file__).parents[2] / "ui" / "pages" / "07_A∕B_Test.py"


def _load_page():
    spec = importlib.util.spec_from_file_location("dashboard_ab_test_page", PAGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _pair(pair_id, baseline, adaptive, ts="1792500900.0"):
    return {
        "pair_id": pair_id,
        "decision_ts": ts,
        "baseline": baseline,
        "adaptive": adaptive,
    }


def test_dashboard_ab_page_exists():
    assert PAGE_PATH.is_file()


def test_selected_day_summary_counts_valid_samples_and_status_mismatches():
    page = _load_page()
    records = [
        _pair("p1", {"status": "BLOCK", "block_reason": "VETO",
                      "hypothetical_exit_price": 100, "exit_price": 101},
              {"status": "PASS", "block_reason": None,
               "hypothetical_exit_price": 102, "exit_price": 103}),
        _pair("p2", {"status": "UNKNOWN"}, {"status": "BLOCK"}),
    ]

    summary = page.summarize_selected_day(records)

    assert summary["total_pairs"] == 2
    assert summary["valid_samples"] == 1
    assert summary["unknown_pairs"] == 1
    assert summary["status_mismatch_count"] == 2
    assert summary["status_mismatch_ratio"] == 1.0
    assert summary["price_comparison_pairs"] == 1
    assert summary["hypothetical_exit_delta_points"] == 2.0
    assert summary["hypothetical_exit_delta_twd"] == 20.0
    assert summary["exit_delta_points"] == 2.0
    assert summary["exit_delta_twd"] == 20.0


def test_missing_values_render_as_na_and_epoch_is_displayed_as_original_taipei_time():
    page = _load_page()
    row = _pair("p1", {"status": "UNKNOWN"}, {"status": "BLOCK"})
    summary = page.summarize_selected_day([row])
    detail = page.build_detail_rows([row])[0]

    assert summary["hypothetical_exit_delta_points"] is None
    assert summary["hypothetical_exit_delta_twd"] is None
    assert summary["exit_delta_points"] is None
    assert detail["adaptive.adaptive_price"] == "N/A"
    assert detail["baseline.vol_anomaly_level"] == "N/A"
    assert detail["時間"] == datetime.fromtimestamp(
        float(row["decision_ts"]), ZoneInfo("Asia/Taipei")
    ).strftime("%Y-%m-%d %H:%M:%S")


def test_missing_paired_file_returns_no_data(tmp_path):
    page = _load_page()
    assert page.load_paired_records("20261020", directory=tmp_path) == []


def test_sim_outcome_reader_filters_pending_events_and_lists_sim_only_day(tmp_path):
    page = _load_page()
    path = tmp_path / "ab_sim_20261020.jsonl"
    path.write_text("\n".join((
        json.dumps({"event_type": "AB_SIM_PENDING", "pair_id": "p1"}),
        json.dumps({"event_type": "AB_SIM_OUTCOME", "pair_id": "p1", "fill_status": "FILLED"}),
    )) + "\n", encoding="utf-8")
    assert page.list_available_days(tmp_path) == ["20261020"]
    assert page.load_sim_outcomes("20261020", directory=tmp_path) == [
        {"event_type": "AB_SIM_OUTCOME", "pair_id": "p1", "fill_status": "FILLED"}
    ]


def test_sim_outcome_summary_rates_and_two_average_differences():
    page = _load_page()
    outcomes = [
        {"fill_status": "FILLED", "diff_points": 2, "diff_twd": 20},
        {"fill_status": "TIMEOUT_MARKET", "diff_points": -2, "diff_twd": -20},
        {"fill_status": "SESSION_END_MARKET", "diff_points": 10, "diff_twd": 100},
        {"fill_status": "ABANDONED_RESTART", "diff_points": None, "diff_twd": None},
        {"fill_status": "INVALID_BBO", "diff_points": None, "diff_twd": None},
    ]
    summary = page.summarize_sim_outcomes(outcomes)
    assert summary["fill_status_distribution"] == {
        "FILLED": 1, "TIMEOUT_MARKET": 1, "SESSION_END_MARKET": 1,
        "ABANDONED_RESTART": 1, "INVALID_BBO": 1,
    }
    assert summary["adaptive_fill_rate"] == pytest.approx(1 / 3)
    assert summary["filled_mean_diff_points"] == 2
    assert summary["filled_mean_diff_twd"] == 20
    assert summary["timeout_inclusive_mean_diff_points"] == 0
    assert summary["timeout_inclusive_mean_diff_twd"] == 0
    assert summary["timeout_inclusive_count"] == 2


def test_conclusion_uses_timeout_inclusive_outcomes_and_no_legacy_complete_pair_claim():
    page = _load_page()
    outcomes = [
        {"fill_status": "FILLED", "diff_points": 3, "diff_twd": 30},
        {"fill_status": "TIMEOUT_MARKET", "diff_points": 1, "diff_twd": 10},
    ]
    result = page.build_conclusion(
        [_pair("p1", {"status": "PASS"}, {"status": "PASS"})],
        sim_outcomes=outcomes,
    )
    assert "adaptive 平均較 baseline 好 2.00 點（20.0 TWD）" in result["conclusion"]
    assert "n=2" in result["conclusion"]


def test_d1_timestamp_note_is_a_fixed_placeholder_and_page_shows_sim_layer():
    page = _load_page()
    source = PAGE_PATH.read_text(encoding="utf-8")
    assert page.D1_DEPLOYMENT_TIME_PLACEHOLDER == "2026-10-20 16:09（台北）"
    assert "之前的紀錄 decision_ts 有 +8 小時偏移（既有缺陷 e8735d466），以原值顯示" in source
    assert "adaptive 成交率" in source
    assert "AB_SIM_OUTCOME" in source
    assert "兩邊皆有出場價的配對" not in source


def test_daily_summary_reports_status_distribution_and_top_block_reasons():
    page = _load_page()
    records = [
        _pair("1", {"status": "BLOCK", "block_reason": "VETO"}, {"status": "BLOCK"}),
        _pair("2", {"status": "BLOCK", "block_reason": "VETO"}, {"status": "PASS"}),
        _pair("3", {"status": "UNKNOWN", "block_reason": "WARMUP"}, {"status": "BLOCK"}),
    ]

    daily = page.build_daily_summary({"20261020": records})[0]

    assert daily["有效樣本數"] == 2
    assert daily["baseline.status 分布"] == "BLOCK: 2, UNKNOWN: 1"
    assert daily["baseline.block_reason Top 3"] == "VETO: 2, WARMUP: 1"


# 2026-10-20 Hermes Agent: synthetic paired rows exercise conclusion branches and exit-side semantics.
def _exit_pair(pair_id, *, delta=2, side="LONG", leg="NEAR"):
    baseline = {
        "status": "PASS",
        "exit_price": 100,
        "hypothetical_release_leg": leg,
        "near": {"side": side},
    }
    adaptive = {
        "status": "PASS",
        "exit_price": 100 + delta,
        "hypothetical_release_leg": leg,
        "near": {"side": side},
    }
    return _pair(pair_id, baseline, adaptive)


def test_conclusion_branch_when_no_valid_samples():
    page = _load_page()
    result = page.build_conclusion([_pair("warmup", {"status": "UNKNOWN"}, {"status": "UNKNOWN"})])
    assert result["conclusion"] == "目前無有效樣本（全為暖機/UNKNOWN），無法下結論。"


def test_conclusion_branch_when_no_paired_exit_prices():
    page = _load_page()
    result = page.build_conclusion([_pair("valid", {"status": "PASS"}, {"status": "PASS"})])
    assert result["conclusion"] == (
        "判定層：兩邊 status 一致率 100.0%（有效樣本 1）。"
        "出場模擬層：尚無 AB_SIM_OUTCOME 紀錄，沒有模擬成交樣本。"
    )


def test_conclusion_branch_for_fewer_than_thirty_exit_pairs():
    page = _load_page()
    result = page.build_conclusion(
        [_exit_pair("sell-leg", delta=2, side="LONG")],
        sim_outcomes=[{"fill_status": "FILLED", "diff_points": 2, "diff_twd": 20}],
    )
    assert result["conclusion"] == (
        "出場模擬層（FILLED + TIMEOUT_MARKET）：adaptive 平均較 baseline 好 2.00 點（20.0 TWD），n=1，樣本不足 30，僅供參考。"
    )


def test_conclusion_branch_for_at_least_thirty_exit_pairs():
    page = _load_page()
    records = [_exit_pair(str(i), delta=-1, side="SHORT") for i in range(30)]
    outcomes = [{"fill_status": "FILLED", "diff_points": 1, "diff_twd": 10}
                for _ in range(30)]
    result = page.build_conclusion(records, sim_outcomes=outcomes)
    assert result["conclusion"] == (
        "出場模擬層（FILLED + TIMEOUT_MARKET）：adaptive 平均較 baseline 好 1.00 點（10.0 TWD），n=30"
    )


def test_conclusion_direction_for_buyback_leg():
    page = _load_page()
    result = page.build_conclusion(
        [_exit_pair("buyback", delta=-3, side="SHORT")],
        sim_outcomes=[{"fill_status": "FILLED", "diff_points": 3, "diff_twd": 30}],
    )
    assert "平均較 baseline 好 3.00 點（30.0 TWD）" in result["conclusion"]


def test_conclusion_uses_normalized_negative_sim_difference_without_leg_side():
    page = _load_page()
    record = _pair("unknown-side", {"status": "PASS"}, {"status": "PASS"})
    result = page.build_conclusion(
        [record], sim_outcomes=[{"fill_status": "TIMEOUT_MARKET", "diff_points": -2, "diff_twd": -20}],
    )
    assert "平均較 baseline 差 2.00 點（20.0 TWD）" in result["conclusion"]


def test_page_filename_and_titles_use_a_b_test():
    assert PAGE_PATH.name == "07_A∕B_Test.py"
    assert PAGE_PATH.is_file()
    source = PAGE_PATH.read_text(encoding="utf-8")
    assert 'page_title="A/B Test"' in source
    assert 'st.title("A/B Test")' in source


def test_conclusion_explanation_is_plain_text_and_covers_method_contract():
    page = _load_page()
    explanation = page.CONCLUSION_EXPLANATION
    assert all(token in explanation for token in (
        "baseline", "adaptive", "判定層", "出場模擬層", "UNKNOWN", "shadow",
        "實際下單", "decision_ts", "原始值",
    ))
    assert not any(char.isdigit() for char in explanation)
