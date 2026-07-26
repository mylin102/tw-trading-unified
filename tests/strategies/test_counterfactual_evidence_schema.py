# 2026-07-26 Gemini CLI: Unit tests for Wave J2-A Counterfactual Evidence Schema
import json
import pytest

from strategies.futures.mts.counterfactual_evidence_schema import (
    CounterfactualTradeFact,
    ExclusionReason,
    FillModel,
    calculate_counterfactual_metrics,
)


def test_counterfactual_trade_fact_immutability_and_validation():
    rec = CounterfactualTradeFact(
        trade_id="TRADE_20260726_001",
        session_date="20260726",
        session="DAY",
        direction="BUY_NEAR_SELL_FAR",
        entry_time="2026-07-26T09:00:00",
        first_trigger_time="2026-07-26T09:15:30",
        actual_final_net_pnl_twd=200.0,
        hypothetical_net_exit_pnl_twd=450.0,
        eligible_for_analysis=True,
        exclusion_reason=ExclusionReason.NONE.value,
    )

    with pytest.raises(Exception):
        rec.actual_final_net_pnl_twd = 300.0  # Must raise FrozenInstanceError

    # Invalid constructor validation: eligible=True requires NONE
    with pytest.raises(ValueError):
        CounterfactualTradeFact(
            trade_id="TRADE_FAIL",
            session_date="20260726",
            session="DAY",
            direction="BUY_NEAR_SELL_FAR",
            entry_time="2026-07-26T09:00:00",
            first_trigger_time=None,
            eligible_for_analysis=True,
            exclusion_reason=ExclusionReason.QUOTE_STALE.value,
        )


def test_calculate_counterfactual_metrics():
    # Case 1: Hypothetical Exit PnL = 450, Actual Final PnL = 200, Actual MFE PnL = 500
    metrics = calculate_counterfactual_metrics(
        hypothetical_net_pnl=450.0,
        actual_final_pnl=200.0,
        actual_mfe_pnl=500.0,
    )

    assert metrics["delta_net_pnl_twd"] == 250.0       # 450 - 200
    assert metrics["ped_actual_twd"] == 300.0          # 500 - 200
    assert metrics["ped_policy_j_twd"] == 50.0         # 500 - 450
    assert metrics["ped_improvement_twd"] == 250.0     # 300 - 50

    # Case 2: Never triggered (Hypothetical Net PnL = None)
    none_metrics = calculate_counterfactual_metrics(
        hypothetical_net_pnl=None,
        actual_final_pnl=200.0,
        actual_mfe_pnl=500.0,
    )
    assert none_metrics["delta_net_pnl_twd"] is None
    assert none_metrics["ped_improvement_twd"] is None
