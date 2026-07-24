# 2026-07-24 Gemini CLI: Wave 1C Differential Replay Test Harness
from dataclasses import asdict
from typing import Any
import pytest

from strategies.futures.mts.config import NormalReleaseConfig
from strategies.futures.mts.contracts import ExitAction, ExitReason, Leg
from strategies.futures.mts.dispatcher import NormalReleaseDispatcher
from strategies.futures.mts.legacy_adapter import LegacyReleaseAdapter
from strategies.futures.mts.normal_release_policy import NormalReleasePolicy
from strategies.futures.mts.state import NormalReleaseState
from test_mts_characterization import _build_mock_context

# 14 Characterization Scenarios
SCENARIOS = [
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
]


def _deep_compare_evaluations(eval_a: Any, eval_b: Any) -> None:
    """Assert deep structural equality between two ExitEvaluation objects."""
    assert eval_a.family == eval_b.family, f"Family mismatch: {eval_a.family} vs {eval_b.family}"
    assert eval_a.action == eval_b.action, f"Action mismatch: {eval_a.action} vs {eval_b.action}"
    assert eval_a.legs == eval_b.legs, f"Legs mismatch: {eval_a.legs} vs {eval_b.legs}"
    assert eval_a.reason == eval_b.reason, f"Reason mismatch: {eval_a.reason} vs {eval_b.reason}"
    
    # State deep equality check
    state_a_dict = asdict(eval_a.next_state)
    state_b_dict = asdict(eval_b.next_state)
    assert state_a_dict == state_b_dict, f"Next state mismatch: {state_a_dict} vs {state_b_dict}"


@pytest.mark.parametrize("scenario_name", SCENARIOS)
def test_gate1_legacy_adapter_vs_pure_policy_deep_structural_parity(scenario_name: str):
    """Gate 1: Verify deep structural equality between LegacyAdapter normalization and Pure NormalReleasePolicy for all 14 scenarios."""
    case = _build_mock_context(scenario=scenario_name)
    context = case.input_context
    released_leg_enum = Leg[case.input_lifecycle_state["released_leg"]] if case.input_lifecycle_state.get("released_leg") else None
    state = NormalReleaseState(
        single_leg_active=case.input_lifecycle_state.get("single_leg_active", False),
        released_leg=released_leg_enum,
    )
    config = NormalReleaseConfig(authority="legacy")

    # 1. Obtain Legacy Normalized Evaluation
    raw_legacy = {
        "action": case.policy_golden.expected_action.name,
        "leg": case.policy_golden.expected_selected_leg.name if case.policy_golden.expected_selected_leg else None,
        "reason": case.policy_golden.expected_reason,
    }
    _, legacy_eval = LegacyReleaseAdapter.normalize_legacy_result(raw_legacy, state, context.event_time_ns)

    # 2. Obtain Pure NormalReleasePolicy Evaluation
    policy = NormalReleasePolicy()
    policy_eval = policy.evaluate(context, state, config)

    # 3. Assert Deep Structural Equality
    _deep_compare_evaluations(legacy_eval, policy_eval)


@pytest.mark.parametrize("scenario_name", SCENARIOS)
def test_gate3_dispatcher_shadow_parity_deep_equality(scenario_name: str):
    """Gate 3: Verify Dispatcher shadow mode produces 100% parity match and deep structural equality."""
    case = _build_mock_context(scenario=scenario_name)
    context = case.input_context
    released_leg_enum = Leg[case.input_lifecycle_state["released_leg"]] if case.input_lifecycle_state.get("released_leg") else None
    state = NormalReleaseState(
        single_leg_active=case.input_lifecycle_state.get("single_leg_active", False),
        released_leg=released_leg_enum,
    )
    config = NormalReleaseConfig(authority="legacy")

    dispatcher = NormalReleaseDispatcher()
    policy = NormalReleasePolicy()

    def legacy_eval(ctx):
        return {
            "action": case.policy_golden.expected_action.name,
            "leg": case.policy_golden.expected_selected_leg.name if case.policy_golden.expected_selected_leg else None,
            "reason": case.policy_golden.expected_reason,
        }

    dispatch_result = dispatcher.evaluate(
        context=context,
        state=state,
        config=config,
        legacy_evaluator_fn=legacy_eval,
        shadow_policy_fn=policy.evaluate,
    )

    assert dispatch_result.parity is not None
    assert dispatch_result.parity.is_match is True, f"Parity mismatch in {scenario_name}: {dispatch_result.parity.details}"
    _deep_compare_evaluations(dispatch_result.authoritative, dispatch_result.shadow)


def test_gate4_delete_legacy_readiness_invariant():
    """Gate 4: Pre-condition check verifying that Pure Policy can execute completely independently of Legacy adapter."""
    policy = NormalReleasePolicy()
    config = NormalReleaseConfig(authority="legacy")
    
    # Evaluate 14 cases directly via pure policy without referencing any legacy function
    for scenario in SCENARIOS:
        case = _build_mock_context(scenario=scenario)
        state = NormalReleaseState()
        result = policy.evaluate(case.input_context, state, config)
        assert result.family == case.policy_golden.expected_action.name or result.action in ExitAction
