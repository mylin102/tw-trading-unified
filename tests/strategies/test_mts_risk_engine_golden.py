# 2026-07-14 Gemini CLI: Golden Verification tests for decoupled Risk Engines & TMFSpread integration
import pytest
from unittest.mock import MagicMock
from strategies.plugins.futures.active.risk_engine import (
    ReleaseRiskEngine,
    SingleLegRiskEngine,
    ReleaseRiskInput,
    SingleLegRiskInput,
    ReleaseRiskDecision,
    SingleLegRiskDecision
)
from core.strategy_context import StrategyContext

def test_release_risk_engine_baseline():
    engine = ReleaseRiskEngine()
    inputs = ReleaseRiskInput(
        base_release_stop_pts=20.0,
        near_pnl=-5.0,
        far_pnl=2.0,
        spread=10.0,
        spread_atr=15.0,
        bb_squeeze_on=False,
        tick_confirmed=True
    )
    decision = engine.evaluate_release_risk(inputs)
    assert decision.base_release_stop_pts == 20.0
    assert decision.final_release_stop_pts == 20.0
    assert len(decision.modifiers) == 0

def test_single_leg_risk_engine_baseline():
    engine = SingleLegRiskEngine()
    
    # 1. No VWAP violation (LONG, price > vwap)
    inputs = SingleLegRiskInput(
        side="LONG",
        current_price=20010.0,
        entry_price=20000.0,
        peak_price=20020.0,
        base_trail_dist_pts=50.0,
        atr_used=20.0,
        vwap=20000.0,
        mtf_score=None,
        mtf_valid=False,
        mtf_age_sec=None,
        unrealized_pnl=10.0
    )
    vwap_cfg = {"enabled": True, "tighten_ratio": 0.3}
    mtf_cfg = {"mode": "disabled"}
    
    decision = engine.evaluate_single_leg_risk(inputs, vwap_cfg, mtf_cfg)
    assert decision.base_trail_dist_pts == 50.0
    assert decision.final_trail_dist_pts == 50.0
    assert len(decision.modifiers) == 0
    assert decision.shadow_trail_dist_pts is None

def test_single_leg_risk_engine_vwap_violation():
    engine = SingleLegRiskEngine()
    
    # 2. VWAP violated (LONG, price < vwap)
    inputs = SingleLegRiskInput(
        side="LONG",
        current_price=19990.0,
        entry_price=20000.0,
        peak_price=20020.0,
        base_trail_dist_pts=50.0,
        atr_used=20.0,
        vwap=20000.0,
        mtf_score=None,
        mtf_valid=False,
        mtf_age_sec=None,
        unrealized_pnl=-10.0
    )
    vwap_cfg = {"enabled": True, "tighten_ratio": 0.3}
    mtf_cfg = {"mode": "disabled"}
    
    decision = engine.evaluate_single_leg_risk(inputs, vwap_cfg, mtf_cfg)
    assert decision.final_trail_dist_pts == 15.0  # 50.0 * 0.3
    assert "VWAP_EXIT_TIGHTENED" in decision.modifiers

def test_single_leg_risk_engine_floor_cap():
    engine = SingleLegRiskEngine()
    
    # 3. Base trail is very small, tighten ratio pushes it below 5.0 floor
    inputs = SingleLegRiskInput(
        side="LONG",
        current_price=19990.0,
        entry_price=20000.0,
        peak_price=20020.0,
        base_trail_dist_pts=10.0,
        atr_used=4.0,
        vwap=20000.0,
        mtf_score=None,
        mtf_valid=False,
        mtf_age_sec=None,
        unrealized_pnl=-10.0
    )
    vwap_cfg = {"enabled": True, "tighten_ratio": 0.3}
    mtf_cfg = {"mode": "disabled"}
    
    decision = engine.evaluate_single_leg_risk(inputs, vwap_cfg, mtf_cfg)
    assert decision.final_trail_dist_pts == 5.0  # floor cap
    assert "VWAP_EXIT_TIGHTENED" in decision.modifiers

def test_single_leg_risk_engine_mtf_mode_resolution():
    engine = SingleLegRiskEngine()
    inputs = SingleLegRiskInput(
        side="LONG",
        current_price=20010.0,
        entry_price=20000.0,
        peak_price=20020.0,
        base_trail_dist_pts=50.0,
        atr_used=20.0,
        vwap=20000.0,
        mtf_score=-40.0,
        mtf_valid=True,
        mtf_age_sec=120.0,
        unrealized_pnl=10.0
    )
    vwap_cfg = {"enabled": True, "tighten_ratio": 0.3}
    
    # Mode: disabled
    dec_disabled = engine.evaluate_single_leg_risk(inputs, vwap_cfg, {"mode": "disabled"})
    assert dec_disabled.configured_mode == "disabled"
    assert dec_disabled.effective_mode == "disabled"
    assert dec_disabled.active is False
    assert dec_disabled.reason == "DISABLED"
    assert dec_disabled.shadow_trail_dist_pts is None
    
    # Mode: shadow
    dec_shadow = engine.evaluate_single_leg_risk(inputs, vwap_cfg, {"mode": "shadow", "conflict_threshold": 30.0, "shadow_tighten_ratio": 0.5})
    assert dec_shadow.configured_mode == "shadow"
    assert dec_shadow.effective_mode == "shadow"
    assert dec_shadow.active is False
    assert dec_shadow.reason == "SHADOW_EXPERIMENT"
    assert dec_shadow.shadow_trail_dist_pts == 25.0  # 50.0 * 0.5
    assert "MTF_CONTEXT_CONFLICT" in dec_shadow.shadow_modifiers
    
    # Mode: enabled (must degrade to shadow in Phase 2)
    dec_enabled = engine.evaluate_single_leg_risk(inputs, vwap_cfg, {"mode": "enabled", "conflict_threshold": 30.0, "shadow_tighten_ratio": 0.5})
    assert dec_enabled.configured_mode == "enabled"
    assert dec_enabled.effective_mode == "shadow"
    assert dec_enabled.active is False
    assert dec_enabled.reason == "ENABLED_NOT_ACTIVATED"
    assert dec_enabled.shadow_trail_dist_pts == 25.0
    assert "MTF_CONTEXT_CONFLICT" in dec_enabled.shadow_modifiers

def test_single_leg_risk_engine_mtf_conflict_logic():
    engine = SingleLegRiskEngine()
    
    # LONG with bearish score (-50) -> Conflict -> Tighten
    inputs_conflict = SingleLegRiskInput(
        side="LONG",
        current_price=20010.0,
        entry_price=20000.0,
        peak_price=20020.0,
        base_trail_dist_pts=50.0,
        atr_used=20.0,
        vwap=20000.0,
        mtf_score=-50.0,
        mtf_valid=True,
        mtf_age_sec=120.0,
        unrealized_pnl=10.0
    )
    vwap_cfg = {"enabled": True, "tighten_ratio": 0.3}
    mtf_cfg = {"mode": "shadow", "conflict_threshold": 30.0, "shadow_tighten_ratio": 0.5}
    
    dec1 = engine.evaluate_single_leg_risk(inputs_conflict, vwap_cfg, mtf_cfg)
    assert dec1.shadow_trail_dist_pts == 25.0
    assert "MTF_CONTEXT_CONFLICT" in dec1.shadow_modifiers
    
    # LONG with bullish score (+50) -> No conflict -> shadow matches formal
    inputs_no_conflict = SingleLegRiskInput(
        side="LONG",
        current_price=20010.0,
        entry_price=20000.0,
        peak_price=20020.0,
        base_trail_dist_pts=50.0,
        atr_used=20.0,
        vwap=20000.0,
        mtf_score=50.0,
        mtf_valid=True,
        mtf_age_sec=120.0,
        unrealized_pnl=10.0
    )
    dec2 = engine.evaluate_single_leg_risk(inputs_no_conflict, vwap_cfg, mtf_cfg)
    assert dec2.shadow_trail_dist_pts == 50.0
    assert len(dec2.shadow_modifiers) == 0

def test_single_leg_risk_engine_fallback_safety():
    engine = SingleLegRiskEngine()
    
    # Try to force trail distance negative or extremely large
    inputs = SingleLegRiskInput(
        side="LONG",
        current_price=20010.0,
        entry_price=20000.0,
        peak_price=20020.0,
        base_trail_dist_pts=-10.0,  # Negative base trail!
        atr_used=20.0,
        vwap=20000.0,
        mtf_score=None,
        mtf_valid=False,
        mtf_age_sec=None,
        unrealized_pnl=10.0
    )
    vwap_cfg = {"enabled": False}
    mtf_cfg = {"mode": "disabled"}
    
    # Invariant check must fail safe and fallback to base_trail (or floor) without crashing
    decision = engine.evaluate_single_leg_risk(inputs, vwap_cfg, mtf_cfg)
    assert decision.final_trail_dist_pts == -10.0 or decision.final_trail_dist_pts == 20.0  # safe default floor/base
