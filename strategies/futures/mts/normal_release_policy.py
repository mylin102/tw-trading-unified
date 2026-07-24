# 2026-07-24 Gemini CLI: Wave 1C Pure NormalReleasePolicy Implementation
from .config import NormalReleaseConfig
from .context_builder import SpreadContext
from .contracts import ExitAction, ExitEvaluation, ExitFamily, ExitReason, Leg
from .policy import ExitPolicy
from .state import NormalReleaseState


class NormalReleasePolicy(ExitPolicy[NormalReleaseState, NormalReleaseConfig]):
    """Pure, stateless NormalReleasePolicy extracted from legacy strategy evaluation logic.
    
    Contains zero broker dependencies, zero wall clock calls, and zero side effects.
    Pure transformation: (SpreadContext, NormalReleaseState, NormalReleaseConfig) -> ExitEvaluation[NormalReleaseState].
    """

    def evaluate(
        self,
        context: SpreadContext,
        state: NormalReleaseState,
        config: NormalReleaseConfig,
    ) -> ExitEvaluation[NormalReleaseState]:
        """Evaluate normal release decision cycle."""
        # 1. Quote & Broker Health Gate (Stale quote check)
        if not context.quote_valid or not context.broker_health_valid:
            return ExitEvaluation(
                family=ExitFamily.NORMAL_RELEASE,
                action=ExitAction.HOLD,
                legs=(),
                reason=ExitReason.QUOTE_STALE,
                next_state=state,
            )

        # 2. Session Force Exit Gate
        if context.session == "FORCE_EXIT":
            return ExitEvaluation(
                family=ExitFamily.NORMAL_RELEASE,
                action=ExitAction.EMERGENCY_FLAT,
                legs=(),
                reason=ExitReason.SESSION_FORCE_FLAT,
                next_state=state,
            )

        # 3. Already Single Leg Active (Trail or Hold)
        if state.single_leg_active:
            # Check if trail exit condition triggered for remaining leg
            if abs(context.spread_z) > 3.0:
                released_legs = (state.released_leg,) if state.released_leg else ()
                return ExitEvaluation(
                    family=ExitFamily.NORMAL_RELEASE,
                    action=ExitAction.TRAIL,
                    legs=released_legs,
                    reason=ExitReason.THRESHOLD_TRIGGERED,
                    next_state=state,
                )
            return ExitEvaluation(
                family=ExitFamily.NORMAL_RELEASE,
                action=ExitAction.HOLD,
                legs=(),
                reason=ExitReason.NONE,
                next_state=state,
            )

        # 4. Normal Release Threshold Trigger Check
        if abs(context.spread_z) >= config.release_atr_ratio:
            # Arbitration: Select leg to release based on spread direction & position side
            selected_leg = self._arbitrate_release_leg(context)
            
            next_state = NormalReleaseState(
                released_leg=selected_leg,
                warmup_started_at_ns=context.event_time_ns,
                release_triggered_at_ns=context.event_time_ns,
                single_leg_active=True,
            )
            return ExitEvaluation(
                family=ExitFamily.NORMAL_RELEASE,
                action=ExitAction.RELEASE,
                legs=(selected_leg,),
                reason=ExitReason.THRESHOLD_TRIGGERED,
                next_state=next_state,
            )

        # Default No-Op HOLD
        return ExitEvaluation(
            family=ExitFamily.NORMAL_RELEASE,
            action=ExitAction.HOLD,
            legs=(),
            reason=ExitReason.NONE,
            next_state=state,
        )

    def _arbitrate_release_leg(self, context: SpreadContext) -> Leg:
        """Helper: Arbitrate which leg to release first based on spread z-score and position side."""
        # Priority arbitration: Near leg released when spread z is positive, Far leg when negative
        if context.spread_z >= 0:
            return Leg.NEAR
        return Leg.FAR
