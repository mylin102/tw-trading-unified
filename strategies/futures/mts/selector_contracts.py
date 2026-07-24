# 2026-07-24 Gemini CLI: Wave 0 Selector Contracts (No premature selector logic)
from dataclasses import dataclass
from enum import Enum
from .contracts import ExitFamily


class SelectionDecision(str, Enum):
    """Result decision of strategy eligibility and selection assessment."""
    SELECT = "SELECT"
    NO_TRADE = "NO_TRADE"
    INSUFFICIENT_CONFIDENCE = "INSUFFICIENT_CONFIDENCE"
    DATA_INVALID = "DATA_INVALID"
    LIQUIDITY_REJECTED = "LIQUIDITY_REJECTED"
    RISK_REJECTED = "RISK_REJECTED"
    EDGE_REJECTED = "EDGE_REJECTED"


@dataclass(frozen=True)
class SelectorDiagnostics:
    """Diagnostic details for regime assessment and strategy selection."""
    selector_version: str
    feature_snapshot_id: str
    normal_score: float
    reverse_score: float
    spread_trail_score: float
    confidence: float
    score_margin: float


@dataclass(frozen=True)
class StrategySelectionResult:
    """Output container for strategy selection evaluation.
    
    Wave 0 pure contract. Selector implementation logic is deferred to Wave 5.
    """
    selected_family: ExitFamily | None
    decision: SelectionDecision
    normal_score: float
    reverse_score: float
    spread_trail_score: float
    confidence: float
    score_margin: float
    eligible_families: tuple[ExitFamily, ...]
    rejected_families: dict[ExitFamily, tuple[str, ...]]
    regime_snapshot_id: str
    feature_snapshot_id: str
    diagnostics: SelectorDiagnostics | None = None
