# 2026-10-20 Hermes Agent spec, implemented by Codex: verify the read-only paired A/B page contract.
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


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
        "出場價層：尚無兩邊皆有出場價的配對，無法比較 adaptive 與 baseline 的出場價優劣。"
    )


def test_conclusion_branch_for_fewer_than_thirty_exit_pairs():
    page = _load_page()
    result = page.build_conclusion([_exit_pair("sell-leg", delta=2, side="LONG")])
    assert result["conclusion"] == (
        "出場價層：adaptive 平均較 baseline 好 2.00 點（20.0 TWD），n=1，樣本不足 30，僅供參考。"
    )


def test_conclusion_branch_for_at_least_thirty_exit_pairs():
    page = _load_page()
    records = [_exit_pair(str(i), delta=-1, side="SHORT") for i in range(30)]
    result = page.build_conclusion(records)
    assert result["conclusion"] == "出場價層：adaptive 平均較 baseline 好 1.00 點（10.0 TWD），n=30"


def test_conclusion_direction_for_buyback_leg():
    page = _load_page()
    result = page.build_conclusion([_exit_pair("buyback", delta=-3, side="SHORT")])
    assert "平均較 baseline 好 3.00 點（30.0 TWD）" in result["conclusion"]


def test_conclusion_direction_is_undetermined_without_consistent_leg_side():
    page = _load_page()
    record = _exit_pair("unknown-side", delta=2)
    record["baseline"].pop("near")
    record["adaptive"].pop("near")
    result = page.build_conclusion([record])
    assert "平均價差 +2.00 點（+20.0 TWD），方向未定" in result["conclusion"]


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
        "baseline", "adaptive", "判定層", "出場價層", "UNKNOWN", "shadow",
        "實際下單", "decision_ts", "原始值",
    ))
    assert not any(char.isdigit() for char in explanation)
