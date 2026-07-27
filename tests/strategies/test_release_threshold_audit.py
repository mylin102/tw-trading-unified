# 2026-07-27 Gemini CLI: Phase 2 Release Threshold & Crossing Diagnostic Tests
import pytest

from strategies.futures.mts.release_threshold_audit import (
    CadenceSource,
    ReleaseThresholdAuditEngine,
)


def test_case_1_uncrossed_threshold():
    # near_pnl = -86, threshold = 90.63 -> near_hit = False, distance = +4.63
    engine = ReleaseThresholdAuditEngine()
    diag = engine.evaluate_crossing(
        trade_id="T001",
        leg="NEAR",
        entry_price=22000.0,
        current_price=22086.0,
        near_pnl_pts=-86.0,
        far_pnl_pts=10.0,
        effective_threshold=90.63,
        trigger_source=CadenceSource.NEAR_TICK,
    )

    assert not diag.near_hit
    assert not diag.threshold_crossed
    assert diag.distance_to_threshold_points == 4.63
    assert diag.final_policy_action == "NO_ACTION"
    assert diag.blocking_gate == "THRESHOLD_NOT_CROSSED"


def test_case_2_exact_threshold_crossing():
    # near_pnl = -90.63, threshold = 90.63 -> near_hit = True, distance = 0.0
    engine = ReleaseThresholdAuditEngine()
    diag = engine.evaluate_crossing(
        trade_id="T001",
        leg="NEAR",
        entry_price=22000.0,
        current_price=22090.63,
        near_pnl_pts=-90.63,
        far_pnl_pts=10.0,
        effective_threshold=90.63,
        trigger_source=CadenceSource.NEAR_TICK,
    )

    assert diag.near_hit
    assert diag.threshold_crossed
    assert diag.distance_to_threshold_points == 0.0
    assert diag.final_policy_action == "RELEASE_NEAR"
    assert diag.blocking_gate is None


def test_case_3_over_threshold():
    # near_pnl = -95.0, threshold = 90.63 -> near_hit = True, distance = -4.37
    engine = ReleaseThresholdAuditEngine()
    diag = engine.evaluate_crossing(
        trade_id="T001",
        leg="NEAR",
        entry_price=22000.0,
        current_price=22095.0,
        near_pnl_pts=-95.0,
        far_pnl_pts=10.0,
        effective_threshold=90.63,
        trigger_source=CadenceSource.NEAR_TICK,
    )

    assert diag.near_hit
    assert diag.threshold_crossed
    assert diag.distance_to_threshold_points == -4.37
    assert diag.final_policy_action == "RELEASE_NEAR"


def test_case_4_profit_must_not_trigger_release():
    # near_pnl = +100.0, threshold = 90.63 -> near_hit = False
    # Prevents regression where abs(pnl) >= threshold was used!
    engine = ReleaseThresholdAuditEngine()
    diag = engine.evaluate_crossing(
        trade_id="T001",
        leg="NEAR",
        entry_price=22000.0,
        current_price=21900.0,
        near_pnl_pts=100.0,
        far_pnl_pts=-10.0,
        effective_threshold=90.63,
        trigger_source=CadenceSource.NEAR_TICK,
    )

    assert not diag.near_hit
    assert not diag.threshold_crossed
    assert diag.distance_to_threshold_points == 190.63
    assert diag.final_policy_action == "NO_ACTION"


def test_case_5_atr_scaling_provenance():
    # base = 60.0, atr = 60.42, mult = 1.5 -> scaled = 90.63
    engine = ReleaseThresholdAuditEngine()
    prov = engine.compute_provenance(
        base_threshold=60.0,
        atr_value=60.42,
        atr_multiplier=1.5,
        min_bound=10.0,
    )

    assert prov.config_source == "ATR_DYNAMIC"
    assert prov.scaled_threshold == 90.63
    assert prov.effective_threshold == 90.63


def test_case_6_evaluation_cadence_source():
    engine = ReleaseThresholdAuditEngine()
    diag = engine.evaluate_crossing(
        trade_id="T001",
        leg="NEAR",
        entry_price=22000.0,
        current_price=22095.0,
        near_pnl_pts=-95.0,
        far_pnl_pts=10.0,
        effective_threshold=90.63,
        trigger_source=CadenceSource.FAR_TICK,
    )

    assert diag.evaluation_trigger_source == CadenceSource.FAR_TICK.value


def test_case_7_prerequisite_named_blocking_gate():
    # near_hit = True but blocked by STALE_QUOTE_AGE
    engine = ReleaseThresholdAuditEngine()
    diag = engine.evaluate_crossing(
        trade_id="T001",
        leg="NEAR",
        entry_price=22000.0,
        current_price=22095.0,
        near_pnl_pts=-95.0,
        far_pnl_pts=10.0,
        effective_threshold=90.63,
        trigger_source=CadenceSource.NEAR_TICK,
        prerequisite_blocking_gate="STALE_QUOTE_AGE",
    )

    assert diag.near_hit
    assert diag.final_policy_action == "NO_ACTION"
    assert diag.blocking_gate == "STALE_QUOTE_AGE"
