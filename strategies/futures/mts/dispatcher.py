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
    ) -> DispatchResult[NormalReleaseState]:
        """Evaluate decision cycle via delegation seam.
        
        Args:
            context: Pure SpreadContext input.
            state: Current NormalReleaseState.
            config: Resolved NormalReleaseConfig (must have authority="legacy").
            legacy_evaluator_fn: Legacy evaluation function (called EXACTLY ONCE).
            shadow_policy_fn: Optional pure policy evaluation function for diagnostic parity.
            
        Returns:
            DispatchResult containing authoritative legacy evaluation, shadow evaluation, and parity.
        """
        if config.authority != "legacy":
            raise ValueError(f"Wave 1B enforces authority='legacy' only, got: {config.authority}")

        # Single Invocation Rule: Call legacy evaluator exactly once
        self.invocation_count += 1
        raw_result: dict[str, Any] | None = None
        exception_raised: Exception | None = None

        try:
            raw_result = legacy_evaluator_fn(context)
        except Exception as exc:
            exception_raised = exc

        # If legacy evaluator raised an exception, normalize observation & re-raise to preserve exception semantics
        if exception_raised is not None:
            obs = LegacyPolicyObservation(
                action=ExitAction.HOLD,
                leg=None,
                reason=None,  # type: ignore
                proposed_transition=None,
                raw_reason=str(exception_raised),
                outcome_kind=OutcomeKind.RAISED,
                exception_type=type(exception_raised).__name__,
                exception_message=str(exception_raised),
            )
            # Re-raise legacy exception without swallowing
            raise exception_raised

        # Normalize raw legacy result into pure ExitEvaluation contract
        obs, authoritative_eval = LegacyReleaseAdapter.normalize_legacy_result(raw_result, state)

        # Shadow Policy Evaluation (diagnostic only, fail-safe)
        shadow_eval: ExitEvaluation[NormalReleaseState] | None = None
        parity_result: ParityResult | None = None

        if shadow_policy_fn is not None:
            try:
                shadow_eval = shadow_policy_fn(context, state, config)
                
                # Compute Parity
                action_match = (authoritative_eval.action == shadow_eval.action)
                leg_match = (authoritative_eval.legs == shadow_eval.legs)
                reason_match = (authoritative_eval.reason == shadow_eval.reason)
                transition_match = (authoritative_eval.next_state.single_leg_active == shadow_eval.next_state.single_leg_active)

                is_match = action_match and leg_match and reason_match and transition_match

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
                    },
                )
            except Exception as shadow_exc:
                # Shadow failure must never block or alter legacy execution
                parity_result = ParityResult(
                    is_match=False,
                    action_match=False,
                    leg_match=False,
                    reason_match=False,
                    transition_match=False,
                    details={"shadow_exception": str(shadow_exc)},
                )

        return DispatchResult(
            authoritative=authoritative_eval,
            shadow=shadow_eval,
            parity=parity_result,
            observation=obs,
        )
