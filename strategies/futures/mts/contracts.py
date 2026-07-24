# 2026-07-24 Gemini CLI: Wave 0 Pure Contracts Definition (ADR-009 / ADR-015 compliant)
from dataclasses import dataclass, field
from enum import Enum
from typing import Generic, TypeVar

StateT = TypeVar("StateT")

# Schema Versions for contract serialization and replay compatibility
CONTEXT_SCHEMA_VERSION: str = "1.0"
POLICY_STATE_SCHEMA_VERSION: str = "1.0"
CONFIG_SCHEMA_VERSION: str = "1.0"
EVALUATION_SCHEMA_VERSION: str = "1.0"


class ExitFamily(str, Enum):
    """MTS Exit Family classification. Immutable after entry selection."""
    NORMAL_RELEASE = "NORMAL_RELEASE"
    REVERSE_HARVEST = "REVERSE_HARVEST"
    SPREAD_PNL_TRAIL = "SPREAD_PNL_TRAIL"


class ExitAction(str, Enum):
    """Pure strategy action intent (uncoupled from broker order types)."""
    HOLD = "HOLD"
    RELEASE = "RELEASE"
    TRAIL = "TRAIL"
    EXIT_BOTH = "EXIT_BOTH"
    EMERGENCY_FLAT = "EMERGENCY_FLAT"


class Leg(str, Enum):
    """Spread leg identifier."""
    NEAR = "NEAR"
    FAR = "FAR"


class Side(str, Enum):
    """Position side / direction."""
    LONG = "LONG"
    SHORT = "SHORT"


class ExitReason(str, Enum):
    """Structured diagnostic reason for exit evaluation."""
    NONE = "NONE"
    THRESHOLD_TRIGGERED = "THRESHOLD_TRIGGERED"
    GIVEBACK_TRIGGERED = "GIVEBACK_TRIGGERED"
    PROFIT_FLOOR_TRIGGERED = "PROFIT_FLOOR_TRIGGERED"
    HARD_STOP_TRIGGERED = "HARD_STOP_TRIGGERED"
    TIMEOUT_TRIGGERED = "TIMEOUT_TRIGGERED"
    QUOTE_STALE = "QUOTE_STALE"
    BROKER_DEGRADED = "BROKER_DEGRADED"
    SESSION_FORCE_FLAT = "SESSION_FORCE_FLAT"


@dataclass(frozen=True)
class ExitDiagnostics:
    """Immutable diagnostic details for policy evaluation tracking."""
    policy_version: str
    parameter_hash: str
    feature_snapshot_id: str = ""
    event_time_ns: int = 0
    metrics: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class ExitEvaluation(Generic[StateT]):
    """Pure evaluation output from ExitPolicy.evaluate().
    
    Contains action intent and next immutable policy state.
    """
    family: ExitFamily
    action: ExitAction
    legs: tuple[Leg, ...]
    reason: ExitReason
    next_state: StateT
    trigger_value: float | None = None
    threshold_value: float | None = None
    diagnostics: ExitDiagnostics | None = None
    schema_version: str = EVALUATION_SCHEMA_VERSION
