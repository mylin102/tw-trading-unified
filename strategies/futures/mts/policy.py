# 2026-07-24 Gemini CLI: Wave 0 Pure ExitPolicy Protocol Definition
from typing import Protocol, TypeVar
from .contracts import ExitEvaluation, ExitFamily
from .context_builder import SpreadContext

StateT = TypeVar("StateT")
ConfigT = TypeVar("ConfigT")


class ExitPolicy(Protocol[StateT, ConfigT]):
    """Pure ExitPolicy Protocol.
    
    Guarantees pure function behavior:
    same (context, state, config) -> same ExitEvaluation(next_state).
    Policy instance MUST NOT implicitly hold mutable state or un-provenanced config.
    """
    family: ExitFamily
    version: str

    def evaluate(
        self,
        context: SpreadContext,
        state: StateT,
        config: ConfigT,
    ) -> ExitEvaluation[StateT]:
        """Evaluate strategy exit rules and return explicit action and next policy state."""
        ...
