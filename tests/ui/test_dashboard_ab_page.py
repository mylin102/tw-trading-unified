# 2026-10-20 Hermes Agent spec, implemented by Codex: verify the read-only paired A/B page contract.
import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


PAGE_PATH = Path(__file__).parents[2] / "ui" / "pages" / "07_ab_test.py"


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
