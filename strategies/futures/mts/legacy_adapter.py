# 2026-07-24 Gemini CLI: Wave 1B Legacy Adapter (Pure observation seam for legacy decision normalization)
from dataclasses import dataclass
from typing import Any
from .contracts import ExitAction, ExitEvaluation, ExitFamily, ExitReason, Leg
from .state import NormalReleaseState


class OutcomeKind:
    """Classification of legacy evaluation execution outcome."""
    RETURNED = "RETURNED"
    RAISED = "RAISED"
    SKIPPED = "SKIPPED"


@dataclass(frozen=True)
class LegacyPolicyObservation:
    """Normalized observation of legacy strategy evaluation output.
    
    Pure observation container — cannot modify legacy behavior or state.
    """
    action: ExitAction
    leg: Leg | None
    reason: ExitReason
    proposed_transition: str | None
    raw_reason: str | None
    outcome_kind: str = OutcomeKind.RETURNED
    exception_type: str | None = None
    exception_message: str | None = None


class LegacyReleaseAdapter:
    """Adapts raw legacy evaluation outputs into pure ExitEvaluation contract."""

    @staticmethod
    def normalize_legacy_result(
        raw_result: dict[str, Any] | None,
        current_state: NormalReleaseState,
        event_time_ns: int | None = None,
    ) -> tuple[LegacyPolicyObservation, ExitEvaluation[NormalReleaseState]]:
        """Normalize raw legacy dictionary output into structured observation and ExitEvaluation."""
        if not raw_result:
            obs = LegacyPolicyObservation(
                action=ExitAction.HOLD,
                leg=None,
                reason=ExitReason.NONE,
                proposed_transition=None,
                raw_reason=None,
                outcome_kind=OutcomeKind.RETURNED,
            )
            eval_result = ExitEvaluation(
                family=ExitFamily.NORMAL_RELEASE,
                action=ExitAction.HOLD,
                legs=(),
                reason=ExitReason.NONE,
                next_state=current_state,
            )
            return obs, eval_result

        raw_action_str = str(raw_result.get("action", "")).upper()
        raw_leg_str = str(raw_result.get("leg", "")).upper() if raw_result.get("leg") else None
        raw_reason = str(raw_result.get("reason", ""))

        # Parse action
        if raw_action_str == "RELEASE":
            action = ExitAction.RELEASE
        elif raw_action_str in ("TRAIL", "EXIT", "FLAT"):
            action = ExitAction.TRAIL
        elif raw_action_str == "EMERGENCY_FLAT":
            action = ExitAction.EMERGENCY_FLAT
        else:
            action = ExitAction.HOLD

        # Parse leg
        if raw_leg_str == "NEAR":
            leg = Leg.NEAR
        elif raw_leg_str == "FAR":
            leg = Leg.FAR
        else:
            leg = None

        # Parse reason
        if "STALE" in raw_reason.upper():
            reason = ExitReason.QUOTE_STALE
        elif "FORCE" in raw_reason.upper() or "SESSION" in raw_reason.upper():
            reason = ExitReason.SESSION_FORCE_FLAT
        elif "TRIGGER" in raw_reason.upper() or action == ExitAction.RELEASE:
            reason = ExitReason.THRESHOLD_TRIGGERED
        else:
            reason = ExitReason.NONE

        proposed_transition = "SINGLE_LEG" if action == ExitAction.RELEASE else None

        obs = LegacyPolicyObservation(
            action=action,
            leg=leg,
            reason=reason,
            proposed_transition=proposed_transition,
            raw_reason=raw_reason,
            outcome_kind=OutcomeKind.RETURNED,
        )

        # Derive next state for NormalReleaseState
        next_released_leg = leg if action == ExitAction.RELEASE else current_state.released_leg
        next_single_leg_active = True if (action in (ExitAction.RELEASE, ExitAction.TRAIL) or current_state.single_leg_active) else False
        
        warmup_ns = event_time_ns if (action == ExitAction.RELEASE and event_time_ns is not None) else current_state.warmup_started_at_ns
        release_ns = event_time_ns if (action == ExitAction.RELEASE and event_time_ns is not None) else current_state.release_triggered_at_ns

        next_state = NormalReleaseState(
            released_leg=next_released_leg,
            warmup_started_at_ns=warmup_ns,
            release_triggered_at_ns=release_ns,
            single_leg_active=next_single_leg_active,
        )

        eval_result = ExitEvaluation(
            family=ExitFamily.NORMAL_RELEASE,
            action=action,
            legs=(leg,) if leg else (),
            reason=reason,
            next_state=next_state,
        )

        return obs, eval_result
