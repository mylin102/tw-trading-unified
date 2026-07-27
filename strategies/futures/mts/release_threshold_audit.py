# 2026-07-27 Gemini CLI: Phase 2 Release Threshold Provenance & Evaluation Cadence Audit
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class CadenceSource(str, Enum):
    NEAR_TICK = "NEAR_TICK"
    FAR_TICK = "FAR_TICK"
    BAR_CALLBACK = "BAR_CALLBACK"
    SCHEDULER = "SCHEDULER"


@dataclass(frozen=True)
class ThresholdProvenance:
    configured_base_threshold: float
    atr_value: Optional[float] = None
    atr_multiplier: Optional[float] = None
    scaled_threshold: Optional[float] = None
    minimum_bound: float = 10.0
    maximum_bound: Optional[float] = None
    effective_threshold: float = 0.0
    config_source: str = "STRATEGY_PARAMS"
    config_version: str = "1.0.0"
    loaded_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ThresholdCrossingDiagnostics:
    trade_id: str
    event_time: str
    leg: str  # "NEAR" or "FAR"
    entry_price: float
    current_price: float
    pnl_points: float
    effective_threshold: float
    distance_to_threshold_points: float  # Signed distance: pnl_points + effective_threshold (Positive = uncrossed, <= 0 = crossed)
    threshold_crossed: bool
    evaluation_trigger_source: str  # CadenceSource value
    price_event_age_ms: float = 0.0
    decision_latency_ms: float = 0.0
    lifecycle_phase: str = "SPREAD"
    near_hit: bool = False
    far_hit: bool = False
    final_policy_action: str = "NO_ACTION"
    blocking_gate: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ReleaseThresholdAuditEngine:
    """
    Evaluates Release Threshold Provenance & Threshold Crossing Diagnostics.
    Enforces signed comparison: near_pnl_pts <= -effective_threshold.
    """

    def compute_provenance(
        self,
        base_threshold: float,
        atr_value: Optional[float] = None,
        atr_multiplier: Optional[float] = None,
        min_bound: float = 10.0,
        max_bound: Optional[float] = None,
    ) -> ThresholdProvenance:
        if atr_value is not None and atr_multiplier is not None and atr_value > 0:
            scaled = round(atr_value * atr_multiplier, 2)
            effective = max(min_bound, scaled)
            if max_bound is not None:
                effective = min(max_bound, effective)
            source = "ATR_DYNAMIC"
        else:
            scaled = None
            effective = base_threshold
            source = "FIXED_FALLBACK"

        return ThresholdProvenance(
            configured_base_threshold=base_threshold,
            atr_value=atr_value,
            atr_multiplier=atr_multiplier,
            scaled_threshold=scaled,
            minimum_bound=min_bound,
            maximum_bound=max_bound,
            effective_threshold=effective,
            config_source=source,
            loaded_at=datetime.now().isoformat(),
        )

    def evaluate_crossing(
        self,
        trade_id: str,
        leg: str,
        entry_price: float,
        current_price: float,
        near_pnl_pts: float,
        far_pnl_pts: float,
        effective_threshold: float,
        trigger_source: CadenceSource,
        prerequisite_blocking_gate: Optional[str] = None,
    ) -> ThresholdCrossingDiagnostics:
        target_pnl = near_pnl_pts if leg == "NEAR" else far_pnl_pts

        # Signed Comparison Contract: near_hit = target_pnl <= -effective_threshold
        near_hit = near_pnl_pts <= -effective_threshold
        far_hit = far_pnl_pts <= -effective_threshold
        threshold_crossed = near_hit if leg == "NEAR" else far_hit

        # Signed Distance for SHORT loss: distance = target_pnl + effective_threshold
        # E.g., near_pnl = -86, threshold = 90.63 -> distance = -86 + 90.63 = +4.63 (Uncrossed)
        distance = round(target_pnl + effective_threshold, 2)

        if threshold_crossed:
            if prerequisite_blocking_gate:
                final_action = "NO_ACTION"
                blocking_gate = prerequisite_blocking_gate
            else:
                final_action = f"RELEASE_{leg}"
                blocking_gate = None
        else:
            final_action = "NO_ACTION"
            blocking_gate = "THRESHOLD_NOT_CROSSED"

        return ThresholdCrossingDiagnostics(
            trade_id=trade_id,
            event_time=datetime.now().isoformat(),
            leg=leg,
            entry_price=entry_price,
            current_price=current_price,
            pnl_points=target_pnl,
            effective_threshold=effective_threshold,
            distance_to_threshold_points=distance,
            threshold_crossed=threshold_crossed,
            evaluation_trigger_source=trigger_source.value,
            near_hit=near_hit,
            far_hit=far_hit,
            final_policy_action=final_action,
            blocking_gate=blocking_gate,
        )
