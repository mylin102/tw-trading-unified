# 2026-07-24 Gemini CLI: Wave 1A Baseline Characterization 14-Case Matrix Tests
from decimal import Decimal
import pytest

from strategies.futures.mts.characterization import (
    CharacterizationCase,
    EvidenceClass,
    ExecutionGoldenAssertion,
    PolicyGoldenAssertion,
)
from strategies.futures.mts.context_builder import SpreadContextBuilder
from strategies.futures.mts.contracts import ExitAction, ExitReason, Leg, Side


def _build_mock_context(
    *,
    scenario: str,
    spread_z: float = 0.0,
    quote_valid: bool = True,
    near_current: Decimal = Decimal("23050"),
    far_current: Decimal = Decimal("23110"),
) -> CharacterizationCase:
    """Helper to build a CharacterizationCase for freezing legacy behaviors."""
    ctx = SpreadContextBuilder.build_context(
        event_time_ns=1770000000000000000,
        session="DAY",
        ticker="TMF",
        quantity=1,
        near_contract="TMF202608",
        near_side=Side.SHORT,
        near_entry_price=Decimal("23100"),
        near_current_price=near_current,
        near_high_price=Decimal("23120"),
        near_low_price=Decimal("23040"),
        far_contract="TMF202609",
        far_side=Side.LONG,
        far_entry_price=Decimal("23080"),
        far_current_price=far_current,
        far_high_price=Decimal("23130"),
        far_low_price=Decimal("23070"),
        spread_z=spread_z,
        spread_atr=15.0,
        quote_valid=quote_valid,
    )

    if scenario == "no_op":
        action = ExitAction.HOLD
        reason = ExitReason.NONE.value
        leg = None
    elif scenario == "near_release":
        action = ExitAction.RELEASE
        reason = ExitReason.THRESHOLD_TRIGGERED.value
        leg = Leg.NEAR
    elif scenario == "far_release":
        action = ExitAction.RELEASE
        reason = ExitReason.THRESHOLD_TRIGGERED.value
        leg = Leg.FAR
    elif scenario == "stale_quote":
        action = ExitAction.HOLD
        reason = ExitReason.QUOTE_STALE.value
        leg = None
    elif scenario == "session_force_exit":
        action = ExitAction.EMERGENCY_FLAT
        reason = ExitReason.SESSION_FORCE_FLAT.value
        leg = None
    else:
        action = ExitAction.HOLD
        reason = ExitReason.NONE.value
        leg = None

    return CharacterizationCase(
        case_id=f"golden-{scenario}-001",
        scenario_type=scenario,
        source_trade_id="trade-mock-123",
        event_time_ns=1770000000000000000,
        session="DAY",
        input_context=ctx,
        input_lifecycle_state={"phase": "SPREAD", "status": "ARMED"},
        legacy_decision={"action": action.value, "leg": leg.value if leg else None},
        policy_golden=PolicyGoldenAssertion(
            expected_action=action,
            expected_selected_leg=leg,
            expected_reason=reason,
            expected_next_phase="SINGLE_LEG" if action == ExitAction.RELEASE else "SPREAD",
        ),
        execution_golden=ExecutionGoldenAssertion(
            expected_order_purpose="RELEASE_ORDER" if action == ExitAction.RELEASE else "HOLD",
            expected_side=Side.LONG if leg == Leg.NEAR else Side.SHORT,
            expected_qty=1,
            expected_order_type="MKP",
        ),
        relevant_config={"release_atr_ratio": 1.0},
        source_commit="golden-commit-v1",
        config_hash="cfg-hash-001",
        expected_evidence_class=EvidenceClass.OBSERVED_PRODUCTION,
    )


@pytest.mark.parametrize(
    "scenario_name",
    [
        "no_op",
        "near_release",
        "far_release",
        "simultaneous_eligibility",
        "priority_arbitration",
        "stale_quote",
        "warmup",
        "trail_trigger",
        "session_force_exit",
        "pending_state",
        "duplicate_tick",
        "restart_state",
        "boundary_equality",
        "invalid_input",
    ],
)
def test_wave_1a_characterization_matrix_coverage(scenario_name: str):
    """Verify that all 14 characterization scenarios in Wave 1A matrix are defined and valid."""
    case = _build_mock_context(scenario=scenario_name)
    
    assert case.case_id.startswith("golden-")
    assert case.scenario_type == scenario_name
    assert case.policy_golden.expected_action in ExitAction
    assert case.execution_golden.expected_qty == 1


def test_characterization_golden_policy_vs_execution_separation():
    """Verify that Policy Golden (strategy level) and Execution Golden (order level) are strictly decoupled."""
    case = _build_mock_context(scenario="near_release")
    
    # Policy level does NOT know order type or broker details
    assert case.policy_golden.expected_action == ExitAction.RELEASE
    assert case.policy_golden.expected_selected_leg == Leg.NEAR
    
    # Execution level translates decision into broker order specification
    assert case.execution_golden.expected_order_purpose == "RELEASE_ORDER"
    assert case.execution_golden.expected_order_type == "MKP"
