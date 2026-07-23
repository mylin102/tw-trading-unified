# 2026-07-14 Gemini CLI: Decoupled Risk Engine layers for ADR-009 Phase 2
from dataclasses import dataclass, field
from typing import Optional, Tuple
import logging

logger = logging.getLogger("MTS_RiskEngine")

@dataclass(frozen=True)
class ReleaseRiskInput:
    base_release_stop_pts: float
    near_pnl: float
    far_pnl: float
    spread: float
    spread_atr: Optional[float] = None
    bb_squeeze_on: bool = False
    tick_confirmed: bool = False

@dataclass(frozen=True)
class SingleLegRiskInput:
    side: str
    current_price: float
    entry_price: float
    peak_price: float
    base_trail_dist_pts: float
    atr_used: Optional[float]
    vwap: Optional[float]
    mtf_score: Optional[float]
    mtf_valid: bool
    mtf_age_sec: Optional[float]
    unrealized_pnl: float
    mfe_pts: float = 0.0

@dataclass(frozen=True)
class ReleaseRiskDecision:
    base_release_stop_pts: float
    final_release_stop_pts: float
    modifiers: Tuple[str, ...]
    shadow_release_stop_pts: Optional[float] = None
    shadow_modifiers: Tuple[str, ...] = ()

@dataclass(frozen=True)
class SingleLegRiskDecision:
    base_trail_dist_pts: float
    final_trail_dist_pts: float
    modifiers: Tuple[str, ...]
    exit_candidate: bool = False
    exit_reason: Optional[str] = None
    shadow_trail_dist_pts: Optional[float] = None
    shadow_modifiers: Tuple[str, ...] = ()
    configured_mode: str = "disabled"
    effective_mode: str = "disabled"
    active: bool = False
    reason: str = "INITIALIZED"

class ReleaseRiskEngine:
    """Risk engine responsible for spread release stop calculations.
    Ensures 100% baseline compatibility in Phase 2.
    """
    def evaluate_release_risk(self, inputs: ReleaseRiskInput) -> ReleaseRiskDecision:
        return ReleaseRiskDecision(
            base_release_stop_pts=inputs.base_release_stop_pts,
            final_release_stop_pts=inputs.base_release_stop_pts,
            modifiers=(),
            shadow_release_stop_pts=inputs.base_release_stop_pts,
            shadow_modifiers=()
        )

class SingleLegRiskEngine:
    """Risk engine responsible for single leg exit & trailing calculations.
    Handles Baseline, Hard Constraints (Profit Lock), Market Structure (VWAP),
    and Context (MTF Shadow) layers separately.
    """
    def evaluate_single_leg_risk(
        self,
        inputs: SingleLegRiskInput,
        vwap_exit_config: dict,
        mtf_config: dict
    ) -> SingleLegRiskDecision:
        modifiers = []
        shadow_modifiers = []
        
        # 1. Baseline Layer
        base_trail = inputs.base_trail_dist_pts
        trail = base_trail
        
        # 2. Hard Constraint Layer (Profit Lock and floor caps)
        # Preserve base floors (e.g. 20.0 pts for trailing stops)
        trail_floor = 20.0
        
        # 3. Market Structure Layer (VWAP - Formal execution)
        if vwap_exit_config.get("enabled", False) and inputs.vwap and inputs.vwap > 0:
            violated = False
            if inputs.side == "LONG" and inputs.current_price < inputs.vwap:
                violated = True
            elif inputs.side == "SHORT" and inputs.current_price > inputs.vwap:
                violated = True
                
            if violated:
                tighten_ratio = float(vwap_exit_config.get("tighten_ratio", 0.3))
                trail = base_trail * tighten_ratio
                # VWAP exit allows tightening below normal floor down to 5.0 points
                trail = max(5.0, trail)
                modifiers.append("VWAP_EXIT_TIGHTENED")
        
        # Enforce baseline floor if not tightened by VWAP
        if "VWAP_EXIT_TIGHTENED" not in modifiers:
            trail = max(trail_floor, trail)
            
        final_value = trail
        
        # Safe Fallback Gates instead of Hard Assertions (Rule 12: Fail loud but don't crash)
        if final_value <= 0:
            logger.error(
                "[MTS_RISK_INVARIANT_VIOLATION] final_value=%s <= 0. "
                "Falling back to base_trail=%s", final_value, base_trail
            )
            final_value = base_trail
            
        if final_value > base_trail:
            logger.error(
                "[MTS_RISK_INVARIANT_VIOLATION] final_value=%s > base_trail=%s. "
                "Falling back to base_trail.", final_value, base_trail
            )
            final_value = base_trail
        
        # 4. Context Layer (MTF - Shadow Mode counterfactual)
        shadow_trail = None
        mtf_mode = mtf_config.get("mode", "disabled")
        
        configured_mode = mtf_mode
        effective_mode = mtf_mode
        active = False
        reason = "INITIALIZED"
        
        # [MTS_MTF_ENABLED_NOT_ACTIVE] semantic guard for Phase 2
        if mtf_mode == "enabled":
            logger.warning(
                "[MTS_MTF_ENABLED_NOT_ACTIVE] "
                "Phase 2 supports shadow calculation only. MTF treated as shadow mode."
            )
            # Log structured resolution details
            logger.info(
                "[MTS_MTF_MODE_RESOLVED] configured=enabled effective=shadow active=false reason=ENABLED_NOT_ACTIVATED"
            )
            effective_mode = "shadow"
            active = False
            reason = "ENABLED_NOT_ACTIVATED"
        elif mtf_mode == "shadow":
            active = False
            reason = "SHADOW_EXPERIMENT"
        else:
            effective_mode = "disabled"
            active = False
            reason = "DISABLED"
            
        if effective_mode == "shadow" and inputs.mtf_valid:
            if inputs.mtf_score is not None:
                conflict_threshold = float(mtf_config.get("conflict_threshold", 30.0))
                shadow_ratio = float(mtf_config.get("shadow_tighten_ratio", 0.5))
                
                conflict = False
                if inputs.side == "LONG" and inputs.mtf_score <= -conflict_threshold:
                    conflict = True
                elif inputs.side == "SHORT" and inputs.mtf_score >= conflict_threshold:
                    conflict = True
                    
                # Always start shadow calculation from the formal final_value
                shadow_trail = final_value
                if conflict:
                    shadow_trail = final_value * shadow_ratio
                    shadow_trail = max(5.0, shadow_trail)
                    shadow_modifiers.append("MTF_CONTEXT_CONFLICT")
                    
                # Defensive constraint gate: shadow must always be tighter than or equal to formal
                if shadow_trail > final_value:
                    logger.error(
                        "[MTS_RISK_INVARIANT_VIOLATION] shadow_trail=%s > final_value=%s. "
                        "Forcing shadow to final_value.", shadow_trail, final_value
                    )
                    shadow_trail = final_value
            else:
                # MTF score is None but valid flag is True -> Treat as neutral (no conflict)
                shadow_trail = final_value
        elif effective_mode == "disabled":
            shadow_trail = None
            shadow_modifiers = []
            
        # Verify invalidity behavior
        if not inputs.mtf_valid:
            shadow_modifiers = []
            if effective_mode != "disabled":
                shadow_trail = final_value
                
        return SingleLegRiskDecision(
            base_trail_dist_pts=base_trail,
            final_trail_dist_pts=final_value,
            modifiers=tuple(modifiers),
            exit_candidate=False,
            exit_reason=None,
            shadow_trail_dist_pts=shadow_trail,
            shadow_modifiers=tuple(shadow_modifiers),
            configured_mode=configured_mode,
            effective_mode=effective_mode,
            active=active,
            reason=reason
        )
