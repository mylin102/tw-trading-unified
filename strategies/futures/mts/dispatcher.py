# 2026-07-24 Gemini CLI: Wave 1B Delegation Seam (NormalReleaseDispatcher with single-invocation isolation & parity)
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar
from .contracts import ExitAction, ExitEvaluation
from .config import NormalReleaseConfig
from .context_builder import SpreadContext
from .legacy_adapter import LegacyPolicyObservation, LegacyReleaseAdapter, OutcomeKind
from .state import NormalReleaseState

StateT = TypeVar("StateT")


@dataclass(frozen=True)
class ParityResult:
    """Parity outcome comparing legacy authoritative decision with pure policy shadow decision."""
    is_match: bool
    action_match: bool
    leg_match: bool
    reason_match: bool
    transition_match: bool
    details: dict[str, Any]


@dataclass(frozen=True)
class DispatchResult(Generic[StateT]):
    """Result of delegation seam evaluation containing authoritative, shadow, parity, and observation data."""
    authoritative: ExitEvaluation[StateT]
    shadow: ExitEvaluation[StateT] | None
    parity: ParityResult | None
    observation: LegacyPolicyObservation


class NormalReleaseDispatcher:
    """Wave 1B Delegation Seam for NormalReleasePolicy.
    
    Guarantees:
    1. Legacy evaluator is called EXACTLY ONCE per decision cycle.
    2. Legacy evaluator is sole authoritative decision source (authority="legacy").
    3. Shadow policy execution failure never blocks or modifies legacy decision.
    4. Legacy exceptions are preserved and re-raised without modification.
    """

    def __init__(self) -> None:
        self.invocation_count: int = 0

    def evaluate(
        self,
        context: SpreadContext,
        state: NormalReleaseState,
        config: NormalReleaseConfig,
        legacy_evaluator_fn: Callable[[SpreadContext], dict[str, Any] | None],
        shadow_policy_fn: Callable[[SpreadContext, NormalReleaseState, NormalReleaseConfig], ExitEvaluation[NormalReleaseState]] | None = None,
        telemetry_logger: Any | None = None,
    ) -> DispatchResult[NormalReleaseState]:
        """Evaluate decision cycle via delegation seam.
        
        Args:
            context: Pure SpreadContext input.
            state: Current NormalReleaseState.
            config: Resolved NormalReleaseConfig (must have authority="legacy").
            legacy_evaluator_fn: Legacy evaluation function (called EXACTLY ONCE).
            shadow_policy_fn: Optional pure policy evaluation function for diagnostic parity.
            telemetry_logger: Optional ProcessSafeTelemetryLogger for non-blocking spooling.
            
        Returns:
            DispatchResult containing authoritative legacy evaluation, shadow evaluation, and parity.
        """
        if config.authority != "legacy":
            raise ValueError(f"Wave 1B enforces authority='legacy' only, got: {config.authority}")

        # Single Invocation Rule: Call legacy evaluator exactly once
        self.invocation_count += 1
        raw_result: dict[str, Any] | None = None
        legacy_exc: Exception | None = None

        try:
            raw_result = legacy_evaluator_fn(context)
        except Exception as exc:
            legacy_exc = exc

        # Also attempt shadow policy evaluation to classify exception matrix
        shadow_eval: ExitEvaluation[NormalReleaseState] | None = None
        shadow_exc: Exception | None = None

        if shadow_policy_fn is not None:
            try:
                shadow_eval = shadow_policy_fn(context, state, config)
            except Exception as s_exc:
                shadow_exc = s_exc

        # If legacy evaluator raised an exception, attempt telemetry enqueue BEFORE re-raising
        if legacy_exc is not None:
            from .telemetry import ParityStatus, ParityTelemetryRecord, compute_canonical_hash
            from dataclasses import asdict

            status = ParityStatus.LEGACY_RAISED_ONLY
            if shadow_exc is not None:
                if type(legacy_exc).__name__ == type(shadow_exc).__name__:
                    status = ParityStatus.BOTH_RAISED_SAME
                else:
                    status = ParityStatus.BOTH_RAISED_DIFFERENT

            if telemetry_logger is not None:
                try:
                    telemetry_logger.record_cycle(
                        ParityTelemetryRecord(
                            record_type="EXCEPTION",
                            decision_cycle_id=f"cycle-{self.invocation_count}",
                            event_time_ns=context.event_time_ns,
                            parity_status=status,
                            context_hash=compute_canonical_hash(asdict(context)),
                            config_hash=compute_canonical_hash(asdict(config)),
                            input_state_hash=compute_canonical_hash(asdict(state)),
                            details={
                                "legacy_exception": str(legacy_exc),
                                "shadow_exception": str(shadow_exc) if shadow_exc else None,
                            },
                        )
                    )
                except Exception:
                    pass  # Telemetry failures must NEVER alter execution or exception flow

            # Re-raise legacy exception without swallowing
            raise legacy_exc

        # Normalize raw legacy result into pure ExitEvaluation contract
        obs, authoritative_eval = LegacyReleaseAdapter.normalize_legacy_result(raw_result, state, context.event_time_ns)

        parity_result: ParityResult | None = None

        if shadow_policy_fn is not None:
            from .telemetry import MismatchDimension, ParityStatus, ParityTelemetryRecord, compute_canonical_hash
            from dataclasses import asdict

            if shadow_exc is not None:
                # Legacy succeeded, but Shadow raised
                parity_result = ParityResult(
                    is_match=False,
                    action_match=False,
                    leg_match=False,
                    reason_match=False,
                    transition_match=False,
                    details={"shadow_exception": str(shadow_exc)},
                )
                if telemetry_logger is not None:
                    try:
                        telemetry_logger.record_cycle(
                            ParityTelemetryRecord(
                                record_type="EXCEPTION",
                                decision_cycle_id=f"cycle-{self.invocation_count}",
                                event_time_ns=context.event_time_ns,
                                parity_status=ParityStatus.POLICY_RAISED_ONLY,
                                context_hash=compute_canonical_hash(asdict(context)),
                                config_hash=compute_canonical_hash(asdict(config)),
                                input_state_hash=compute_canonical_hash(asdict(state)),
                                legacy_action=authoritative_eval.action.name,
                                legacy_reason=authoritative_eval.reason.name,
                                details={"shadow_exception": str(shadow_exc)},
                            )
                        )
                    except Exception:
                        pass
            elif shadow_eval is not None:
                # Both Legacy and Shadow succeeded -> Compute Parity & Mismatch Dimensions
                action_match = (authoritative_eval.action == shadow_eval.action)
                leg_match = (authoritative_eval.legs == shadow_eval.legs)
                reason_match = (authoritative_eval.reason == shadow_eval.reason)
                transition_match = (authoritative_eval.next_state.single_leg_active == shadow_eval.next_state.single_leg_active)
                state_match = (asdict(authoritative_eval.next_state) == asdict(shadow_eval.next_state))

                is_match = action_match and leg_match and reason_match and transition_match and state_match

                mismatch_dims: list[str] = []
                if not action_match:
                    mismatch_dims.append(MismatchDimension.ACTION_MISMATCH.value)
                if not leg_match:
                    mismatch_dims.append(MismatchDimension.LEG_MISMATCH.value)
                if not reason_match:
                    mismatch_dims.append(MismatchDimension.REASON_MISMATCH.value)
                if not state_match:
                    mismatch_dims.append(MismatchDimension.STATE_MISMATCH.value)

                parity_result = ParityResult(
                    is_match=is_match,
                    action_match=action_match,
                    leg_match=leg_match,
                    reason_match=reason_match,
                    transition_match=transition_match,
                    details={
                        "auth_action": authoritative_eval.action.name,
                        "shadow_action": shadow_eval.action.name,
                        "auth_legs": [l.name for l in authoritative_eval.legs],
                        "shadow_legs": [l.name for l in shadow_eval.legs],
                        "auth_reason": authoritative_eval.reason.name,
                        "shadow_reason": shadow_eval.reason.name,
                        "state_match": state_match,
                        "auth_state": asdict(authoritative_eval.next_state),
                        "shadow_state": asdict(shadow_eval.next_state),
                        "mismatch_dimensions": mismatch_dims,
                    },
                )

                if telemetry_logger is not None:
                    try:
                        telemetry_logger.record_cycle(
                            ParityTelemetryRecord(
                                record_type="MATCH" if is_match else "MISMATCH",
                                decision_cycle_id=f"cycle-{self.invocation_count}",
                                event_time_ns=context.event_time_ns,
                                parity_status=ParityStatus.MATCH if is_match else ParityStatus.MISMATCH,
                                mismatch_dimensions=mismatch_dims,
                                context_hash=compute_canonical_hash(asdict(context)),
                                config_hash=compute_canonical_hash(asdict(config)),
                                input_state_hash=compute_canonical_hash(asdict(state)),
                                legacy_action=authoritative_eval.action.name,
                                shadow_action=shadow_eval.action.name,
                                legacy_reason=authoritative_eval.reason.name,
                                shadow_reason=shadow_eval.reason.name,
                            )
                        )
                    except Exception:
                        pass
        elif telemetry_logger is not None:
            # Shadow not enabled -> Record SHADOW_SKIPPED
            try:
                from .telemetry import ParityStatus, ParityTelemetryRecord, compute_canonical_hash
                from dataclasses import asdict

                telemetry_logger.record_cycle(
                    ParityTelemetryRecord(
                        record_type="SKIPPED",
                        decision_cycle_id=f"cycle-{self.invocation_count}",
                        event_time_ns=context.event_time_ns,
                        parity_status=ParityStatus.SHADOW_SKIPPED,
                        context_hash=compute_canonical_hash(asdict(context)),
                        config_hash=compute_canonical_hash(asdict(config)),
                        input_state_hash=compute_canonical_hash(asdict(state)),
                        legacy_action=authoritative_eval.action.name,
                        legacy_reason=authoritative_eval.reason.name,
                    )
                )
            except Exception:
                pass

        return DispatchResult(
            authoritative=authoritative_eval,
            shadow=shadow_eval,
            parity=parity_result,
            observation=obs,
        )
