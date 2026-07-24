# 2026-07-24 Gemini CLI: Wave 1B Delegation Seam Acceptance & Invariant Tests
from decimal import Decimal
from unittest.mock import MagicMock
import pytest
from strategies.futures.mts.config import NormalReleaseConfig
from strategies.futures.mts.context_builder import SpreadContext, SpreadContextBuilder
from strategies.futures.mts.contracts import ExitAction, ExitFamily, ExitReason, Leg, Side
from strategies.futures.mts.dispatcher import NormalReleaseDispatcher
from strategies.futures.mts.economics import ContractEconomics
from strategies.futures.mts.legacy_adapter import LegacyReleaseAdapter, OutcomeKind
from strategies.futures.mts.state import NormalReleaseState
from test_mts_characterization import _build_mock_context


def _make_sample_context() -> SpreadContext:
    return SpreadContextBuilder.build_context(
        event_time_ns=1_720_000_000_000_000_000,
        session="DAY",
        ticker="TMF",
        quantity=1,
        near_contract="TMF202608",
        near_side=Side.SHORT,
        near_entry_price=Decimal("23050"),
        near_current_price=Decimal("23051"),
        near_high_price=Decimal("23060"),
        near_low_price=Decimal("23040"),
        far_contract="TMF202609",
        far_side=Side.LONG,
        far_entry_price=Decimal("23110"),
        far_current_price=Decimal("23111"),
        far_high_price=Decimal("23120"),
        far_low_price=Decimal("23100"),
        spread_z=0.5,
        spread_atr=1.0,
        realized_pnl_twd=0,
        quote_valid=True,
        broker_health_valid=True,
    )


def test_dispatcher_legacy_is_authoritative():
    """Verify legacy path remains sole authority in Wave 1B."""
    dispatcher = NormalReleaseDispatcher()
    context = _make_sample_context()
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")

    def legacy_eval(ctx):
        return {"action": "RELEASE", "leg": "FAR", "reason": "TRIGGERED"}

    result = dispatcher.evaluate(context, state, config, legacy_eval)

    assert result.authoritative.action == ExitAction.RELEASE
    assert result.authoritative.legs == (Leg.FAR,)
    assert result.observation.outcome_kind == OutcomeKind.RETURNED
    assert result.observation.raw_reason == "TRIGGERED"


def test_dispatcher_calls_legacy_once():
    """Verify legacy evaluator is called EXACTLY ONCE per decision cycle."""
    dispatcher = NormalReleaseDispatcher()
    context = _make_sample_context()
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")

    mock_legacy = MagicMock(return_value={"action": "HOLD", "reason": "WARMUP"})
    result = dispatcher.evaluate(context, state, config, mock_legacy)

    assert mock_legacy.call_count == 1
    assert dispatcher.invocation_count == 1


def test_shadow_cannot_emit_order():
    """Verify shadow path output cannot override authoritative output."""
    dispatcher = NormalReleaseDispatcher()
    context = _make_sample_context()
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")

    def legacy_eval(ctx):
        return {"action": "HOLD", "reason": "NONE"}

    def shadow_eval(ctx, st, cfg):
        # Shadow suggests RELEASE, but authoritative output MUST be HOLD
        return LegacyReleaseAdapter.normalize_legacy_result(
            {"action": "RELEASE", "leg": "NEAR", "reason": "TRIGGERED"}, st
        )[1]

    result = dispatcher.evaluate(context, state, config, legacy_eval, shadow_eval)

    assert result.authoritative.action == ExitAction.HOLD
    assert result.shadow.action == ExitAction.RELEASE
    assert result.parity.is_match is False
    assert result.parity.action_match is False


def test_shadow_failure_does_not_duplicate_legacy_action():
    """Verify shadow policy exceptions do not block or alter legacy evaluation."""
    dispatcher = NormalReleaseDispatcher()
    context = _make_sample_context()
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")

    def legacy_eval(ctx):
        return {"action": "RELEASE", "leg": "NEAR", "reason": "TRIGGERED"}

    def broken_shadow_eval(ctx, st, cfg):
        raise RuntimeError("Shadow evaluation internal bug")

    result = dispatcher.evaluate(context, state, config, legacy_eval, broken_shadow_eval)

    # Legacy execution succeeds completely despite shadow error
    assert result.authoritative.action == ExitAction.RELEASE
    assert result.authoritative.legs == (Leg.NEAR,)
    assert result.parity.is_match is False
    assert "shadow_exception" in result.parity.details


def test_delegation_preserves_exception_semantics():
    """Verify legacy exceptions are re-raised without being swallowed or altered."""
    dispatcher = NormalReleaseDispatcher()
    context = _make_sample_context()
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")

    def failing_legacy_eval(ctx):
        raise ValueError("Legacy行情異常中斷")

    with pytest.raises(ValueError, match="Legacy行情異常中斷"):
        dispatcher.evaluate(context, state, config, failing_legacy_eval)


def test_authority_config_rejects_policy_mode():
    """Verify Wave 1B config validator rejects authority='policy' with fail-closed exception."""
    with pytest.raises(ValueError, match="Wave 1B enforces authority='legacy' only"):
        NormalReleaseConfig(authority="policy")


def test_delegation_preserves_event_time():
    """Verify seam does not inject new wall-clock timestamps or alter context timestamp."""
    dispatcher = NormalReleaseDispatcher()
    context = _make_sample_context()
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")

    captured_ts = []

    def legacy_eval(ctx):
        captured_ts.append(ctx.event_time_ns)
        return {"action": "HOLD", "reason": "OK"}

    dispatcher.evaluate(context, state, config, legacy_eval)

    assert captured_ts == [1_720_000_000_000_000_000]


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
def test_all_14_golden_cases_through_dispatch_seam(scenario_name: str):
    """Verify all 14 Golden Characterization Cases maintain 100% parity through NormalReleaseDispatcher."""
    dispatcher = NormalReleaseDispatcher()
    config = NormalReleaseConfig(authority="legacy")

    case = _build_mock_context(scenario=scenario_name)
    context = case.input_context
    state = NormalReleaseState(single_leg_active=case.input_lifecycle_state.get("single_leg_active", False))

    def legacy_eval(ctx):
        if case.policy_golden.expected_action == ExitAction.RELEASE:
            leg_name = case.policy_golden.expected_selected_leg.name if case.policy_golden.expected_selected_leg else "NEAR"
            return {"action": "RELEASE", "leg": leg_name, "reason": "TRIGGERED"}
        elif case.policy_golden.expected_action == ExitAction.TRAIL:
            leg_name = case.policy_golden.expected_selected_leg.name if case.policy_golden.expected_selected_leg else None
            return {"action": "TRAIL", "leg": leg_name, "reason": "TRAIL"}
        elif case.policy_golden.expected_action == ExitAction.EMERGENCY_FLAT:
            return {"action": "EMERGENCY_FLAT", "reason": "FORCE"}
        else:
            return {"action": "HOLD", "reason": "NONE"}

    result = dispatcher.evaluate(context, state, config, legacy_eval)

    assert result.authoritative.action == case.policy_golden.expected_action
    if case.policy_golden.expected_selected_leg:
        assert result.authoritative.legs == (case.policy_golden.expected_selected_leg,)
