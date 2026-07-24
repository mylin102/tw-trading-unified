# 2026-07-24 Gemini CLI: Wave 1A Characterization & Baseline Capture Models
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from .contracts import ExitAction, Leg, Side
from .context_builder import SpreadContext


class EvidenceClass(str, Enum):
    """Evidence class for characterization cases."""
    OBSERVED_PRODUCTION = "OBSERVED_PRODUCTION"
    HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
    SYNTHETIC_BOUNDARY = "SYNTHETIC_BOUNDARY"


@dataclass(frozen=True)
class PolicyGoldenAssertion:
    """Policy Golden Assertion (Pure Decision Level).
    
    Context + State + Config -> Action + Leg + Reason + Proposed Transition.
    Decoupled from execution order types or broker mechanics.
    """
    expected_action: ExitAction
    expected_selected_leg: Leg | None
    expected_reason: str
    expected_next_phase: str


@dataclass(frozen=True)
class ExecutionGoldenAssertion:
    """Execution Golden Assertion (Translator / Coordinator Level).
    
    Committed Decision + Position Snapshot -> Order Purpose + Side + Qty + Order Type.
    """
    expected_order_purpose: str
    expected_side: Side
    expected_qty: int
    expected_order_type: str


@dataclass(frozen=True)
class CharacterizationCase:
    """Characterization Case container for freezing legacy behavior.
    
    Wave 1A: Freezes legacy decision behavior without modifying legacy code or runtime authority.
    """
    case_id: str
    scenario_type: str  # e.g. "no_op", "near_release", "far_release", "stale_quote", etc.
    source_trade_id: str
    event_time_ns: int
    session: str
    input_context: SpreadContext
    input_lifecycle_state: dict[str, Any]
    legacy_decision: dict[str, Any]
    policy_golden: PolicyGoldenAssertion
    execution_golden: ExecutionGoldenAssertion
    relevant_config: dict[str, Any]
    source_commit: str
    config_hash: str
    expected_evidence_class: EvidenceClass = EvidenceClass.OBSERVED_PRODUCTION
    metadata: dict[str, Any] = field(default_factory=dict)
