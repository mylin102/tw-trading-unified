# 2026-07-27 Gemini CLI: Unit tests for PolicyJValidationReportEngine
import pytest

from strategies.futures.mts.policy_j_validation_report import (
    GateResult,
    PolicyJValidationReportEngine,
    Recommendation,
)


def test_validation_report_insufficient_data():
    # 2 dates < 3 dates -> INSUFFICIENT_DATA
    shadow_snaps = []
    outcomes = [
        {"trade_id": "T1", "session_date": "20260725", "actual_final_net_pnl_twd": 100.0},
        {"trade_id": "T2", "session_date": "20260726", "actual_final_net_pnl_twd": 200.0},
    ]

    engine = PolicyJValidationReportEngine()
    report, details = engine.generate_report(shadow_snaps, outcomes)

    assert report.final_recommendation == Recommendation.CONTINUE_SHADOW_COLLECTION.value
    assert all(g.result == GateResult.INSUFFICIENT_DATA.value for g in report.gates)


def test_validation_report_full_pipeline_pass():
    # 5 trading dates
    dates = ["20260721", "20260722", "20260723", "20260724", "20260725"]
    outcomes = [
        {
            "trade_id": f"TRADE_{i:03d}",
            "session_date": dates[i % 5],
            "session": "DAY" if i % 2 == 0 else "NIGHT",
            "direction": "BUY_NEAR_SELL_FAR",
            "entry_time": "2026-07-26T09:00:00",
            "actual_final_net_pnl_twd": 100.0,
            "actual_mfe_net_pnl_twd": 500.0,
        }
        for i in range(15)
    ]
    shadow_snaps = [
        {
            "mode": "SHADOW_ONLY",
            "trade_id": f"TRADE_{i:03d}",
            "event_time": "2026-07-26T09:00:00",
            "eligible": True,
            "eligibility_reason": "HEDGED_PAIR_SPREAD",
            "gross_liquidation_pnl_twd": 442.0,
            "estimated_friction_twd": 92.0,
            "estimated_net_exit_pnl_twd": 350.0,
            "near_quote_age_ms": 10.0,
            "far_quote_age_ms": 10.0,
        }
        for i in range(15)
    ] + [
        {
            "mode": "SHADOW_ONLY",
            "trade_id": f"TRADE_{i:03d}",
            "event_time": "2026-07-26T09:05:00",
            "eligible": True,
            "eligibility_reason": "HEDGED_PAIR_SPREAD",
            "gross_liquidation_pnl_twd": 312.0,
            "estimated_friction_twd": 92.0,
            "estimated_net_exit_pnl_twd": 220.0,
            "near_quote_age_ms": 10.0,
            "far_quote_age_ms": 10.0,
        }
        for i in range(15)
    ]

    engine = PolicyJValidationReportEngine()
    report, details = engine.generate_report(shadow_snaps, outcomes)

    assert report.total_trades_count == 15
    assert len(report.gates) == 10
    # Holdout total delta > 0, all gates PASS
    assert report.final_recommendation == Recommendation.ADVANCE_TO_EXECUTION_DESIGN.value
