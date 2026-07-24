# 2026-07-24 Gemini CLI: Wave 0 MTS Pure Contracts & ACL Module
"""MTS Multi-Exit Strategy Pure Domain Model & Anti-Corruption Layer."""

from .contracts import ExitAction, ExitDiagnostics, ExitEvaluation, ExitFamily, Leg, Side
from .economics import ContractEconomics
from .dispatcher import DispatchResult, NormalReleaseDispatcher, ParityResult
from .legacy_adapter import LegacyPolicyObservation, LegacyReleaseAdapter, OutcomeKind

__all__ = [
    "ExitFamily",
    "ExitAction",
    "Leg",
    "Side",
    "ExitDiagnostics",
    "ExitEvaluation",
    "ContractEconomics",
    "ExitPolicy",
    "LegacyPolicyObservation",
    "LegacyReleaseAdapter",
    "NormalReleaseDispatcher",
    "DispatchResult",
    "ParityResult",
    "OutcomeKind",
]
