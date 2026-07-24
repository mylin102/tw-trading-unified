# 2026-07-24 Gemini CLI: Wave 1D.2 Runtime Integration Verification Tests
from decimal import Decimal
from pathlib import Path
import pytest

from strategies.futures.mts.config import NormalReleaseConfig
from strategies.futures.mts.context_builder import SpreadContext
from strategies.futures.mts.contracts import ExitAction, ExitReason, Leg, Side
from strategies.futures.mts.dispatcher import NormalReleaseDispatcher
from strategies.futures.mts.normal_release_policy import NormalReleasePolicy
from strategies.futures.mts.state import NormalReleaseState
from strategies.futures.mts.telemetry import ParityStatus, ProcessSafeTelemetryLogger
from test_mts_characterization import _build_mock_context


def test_runtime_integration_legacy_called_once():
    """Verify legacy evaluator is called EXACTLY ONCE per decision cycle."""
    dispatcher = NormalReleaseDispatcher()
    context = _build_mock_context(scenario="no_op").input_context
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")
    calls = []

    def mock_legacy(ctx):
        calls.append(ctx)
        return {"action": "HOLD"}

    dispatcher.evaluate(context, state, config, legacy_evaluator_fn=mock_legacy)

    assert len(calls) == 1
    assert dispatcher.invocation_count == 1


def test_runtime_integration_shadow_called_once():
    """Verify pure policy shadow evaluator is called AT MOST ONCE per decision cycle."""
    dispatcher = NormalReleaseDispatcher()
    context = _build_mock_context(scenario="no_op").input_context
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")
    shadow_calls = []

    def mock_legacy(ctx):
        return {"action": "HOLD"}

    def mock_shadow(ctx, st, cfg):
        shadow_calls.append((ctx, st, cfg))
        return NormalReleasePolicy.evaluate(ctx, st, cfg)

    dispatcher.evaluate(context, state, config, legacy_evaluator_fn=mock_legacy, shadow_policy_fn=mock_shadow)

    assert len(shadow_calls) == 1


def test_runtime_integration_order_intents_unchanged():
    """Verify shadow policy execution does NOT create or alter order intents."""
    dispatcher = NormalReleaseDispatcher()
    context = _build_mock_context(scenario="no_op").input_context
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")

    def mock_legacy(ctx):
        return {"action": "RELEASE", "leg": "NEAR", "reason": "TRIGGERED"}

    res_without_shadow = dispatcher.evaluate(context, state, config, legacy_evaluator_fn=mock_legacy)
    res_with_shadow = dispatcher.evaluate(
        context, state, config, legacy_evaluator_fn=mock_legacy, shadow_policy_fn=NormalReleasePolicy.evaluate
    )

    assert res_without_shadow.authoritative.action == res_with_shadow.authoritative.action
    assert res_without_shadow.authoritative.legs == res_with_shadow.authoritative.legs
    assert res_with_shadow.authoritative.action == ExitAction.RELEASE


def test_runtime_integration_state_commits_unchanged():
    """Verify shadow evaluation produces zero side-effects on original input state object."""
    dispatcher = NormalReleaseDispatcher()
    context = _build_mock_context(scenario="no_op").input_context
    state = NormalReleaseState(single_leg_active=False)
    config = NormalReleaseConfig(authority="legacy")

    dispatcher.evaluate(
        context, state, config, legacy_evaluator_fn=lambda c: {"action": "HOLD"}, shadow_policy_fn=NormalReleasePolicy.evaluate
    )

    # Input state remains untouched
    assert state.single_leg_active is False


def test_runtime_integration_lifecycle_events_unchanged():
    """Verify shadow evaluation does not alter observation outcome kind."""
    dispatcher = NormalReleaseDispatcher()
    context = _build_mock_context(scenario="no_op").input_context
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")

    res = dispatcher.evaluate(
        context, state, config, legacy_evaluator_fn=lambda c: {"action": "HOLD"}, shadow_policy_fn=NormalReleasePolicy.evaluate
    )

    assert res.observation.action == ExitAction.HOLD


def test_runtime_integration_policy_exception_isolated():
    """Verify exception in shadow policy is caught safely and does NOT block legacy result."""
    dispatcher = NormalReleaseDispatcher()
    context = _build_mock_context(scenario="no_op").input_context
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")

    def faulty_shadow(ctx, st, cfg):
        raise RuntimeError("Faulty shadow error")

    res = dispatcher.evaluate(
        context, state, config, legacy_evaluator_fn=lambda c: {"action": "HOLD"}, shadow_policy_fn=faulty_shadow
    )

    assert res.authoritative.action == ExitAction.HOLD
    assert res.parity is not None
    assert res.parity.is_match is False
    assert "faulty shadow error" in res.parity.details["shadow_exception"].lower()


def test_runtime_integration_legacy_exception_preserved(tmp_path: Path):
    """Verify legacy exception is re-raised and telemetry is enqueued BEFORE re-raising."""
    dispatcher = NormalReleaseDispatcher()
    context = _build_mock_context(scenario="no_op").input_context
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")
    logger = ProcessSafeTelemetryLogger(tmp_path / "spool", queue_maxsize=100)

    def crashing_legacy(ctx):
        raise ValueError("Legacy crash!")

    def non_crashing_shadow(ctx, st, cfg):
        return NormalReleasePolicy.evaluate(ctx, st, cfg)

    with pytest.raises(ValueError, match="Legacy crash!"):
        dispatcher.evaluate(
            context,
            state,
            config,
            legacy_evaluator_fn=crashing_legacy,
            shadow_policy_fn=None,  # Shadow not called/raised -> LEGACY_RAISED_ONLY
            telemetry_logger=logger,
        )

    summary = logger.get_evaluation_summary()
    assert summary.legacy_raised_only == 1
    assert summary.cycles_seen == 1
    logger.stop()


def test_runtime_integration_both_raised_classification(tmp_path: Path):
    """Verify BOTH_RAISED_SAME status when both legacy and shadow raise same exception type."""
    dispatcher = NormalReleaseDispatcher()
    context = _build_mock_context(scenario="no_op").input_context
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")
    logger = ProcessSafeTelemetryLogger(tmp_path / "spool", queue_maxsize=100)

    def crashing_legacy(ctx):
        raise ValueError("Same crash")

    def crashing_shadow(ctx, st, cfg):
        raise ValueError("Same crash")

    with pytest.raises(ValueError):
        dispatcher.evaluate(
            context,
            state,
            config,
            legacy_evaluator_fn=crashing_legacy,
            shadow_policy_fn=crashing_shadow,
            telemetry_logger=logger,
        )

    summary = logger.get_evaluation_summary()
    assert summary.both_raised_same == 1
    logger.stop()


def test_runtime_integration_single_record_per_cycle(tmp_path: Path):
    """Verify exactly ONE telemetry record is enqueued per decision cycle."""
    dispatcher = NormalReleaseDispatcher()
    context = _build_mock_context(scenario="no_op").input_context
    state = NormalReleaseState()
    config = NormalReleaseConfig(authority="legacy")
    logger = ProcessSafeTelemetryLogger(tmp_path / "spool", queue_maxsize=100)

    dispatcher.evaluate(
        context,
        state,
        config,
        legacy_evaluator_fn=lambda c: {"action": "HOLD"},
        shadow_policy_fn=lambda c, s, cfg: NormalReleasePolicy.evaluate(c, s, cfg),
        telemetry_logger=logger,
    )

    summary = logger.get_evaluation_summary()
    assert summary.cycles_seen == 1
    assert summary.is_accounted is True
    logger.stop()


def test_runtime_integration_restart_reinitializes_logger_identity(tmp_path: Path):
    """Verify new logger instance produces distinct spool file path identity upon restart."""
    base_dir = tmp_path / "spool"
    logger1 = ProcessSafeTelemetryLogger(base_dir, deployment_id="deploy-v1")
    time_path1 = logger1.spool_path
    logger1.stop()

    logger2 = ProcessSafeTelemetryLogger(base_dir, deployment_id="deploy-v1")
    time_path2 = logger2.spool_path
    logger2.stop()

    assert time_path1.name != time_path2.name, "Restart should generate distinct spool file identity!"
