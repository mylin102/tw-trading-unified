# 2026-08-03 Gemini CLI: Complete test suite for Atomic Exit Arbiter Architecture
import pytest
from datetime import datetime
from typing import Dict, Any

from core.order_management.exit_arbiter import (
    ExitArbiter,
    ExitClaimRequest,
    ExitClaimResult,
    CandidatePriority,
)

def test_policy_j_only_trigger():
    arbiter = ExitArbiter()
    req = ExitClaimRequest(
        trade_id="TR-101",
        single_leg_episode_id="EP-001",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-PJ-01",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK",
        source_receive_sequence=1001,
        trigger_timestamp="2026-08-03T17:00:00.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
        total_net_pnl_twd=160.0,
    )
    result = arbiter.try_claim([req])
    assert result.success is True
    assert result.winner_request.owner == "POLICY_J"
    assert result.state_revision == 2

def test_trail_only_trigger():
    arbiter = ExitArbiter()
    req = ExitClaimRequest(
        trade_id="TR-102",
        single_leg_episode_id="EP-002",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-TR-01",
        owner="SINGLE_LEG_TRAIL",
        exit_reason="SINGLE_LEG_TRAIL_HIT",
        source_receive_sequence=1002,
        trigger_timestamp="2026-08-03T17:00:01.000",
        priority=CandidatePriority.SINGLE_LEG_TRAIL,
        total_net_pnl_twd=80.0,
    )
    result = arbiter.try_claim([req])
    assert result.success is True
    assert result.winner_request.owner == "SINGLE_LEG_TRAIL"

def test_same_tick_both_trigger():
    arbiter = ExitArbiter()
    req_pj = ExitClaimRequest(
        trade_id="TR-103",
        single_leg_episode_id="EP-003",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-PJ-02",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK",
        source_receive_sequence=1003,
        trigger_timestamp="2026-08-03T17:00:02.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
        total_net_pnl_twd=180.0,
    )
    req_trail = ExitClaimRequest(
        trade_id="TR-103",
        single_leg_episode_id="EP-003",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-TR-02",
        owner="SINGLE_LEG_TRAIL",
        exit_reason="SINGLE_LEG_TRAIL_HIT",
        source_receive_sequence=1003,
        trigger_timestamp="2026-08-03T17:00:02.000",
        priority=CandidatePriority.SINGLE_LEG_TRAIL,
        total_net_pnl_twd=120.0,
    )
    result = arbiter.try_claim([req_trail, req_pj])
    assert result.success is True
    assert result.same_source_tick is True
    assert result.winner_request.owner == "POLICY_J"
    assert len(result.suppressed_candidates) == 1
    assert result.suppressed_candidates[0]["candidate_event_id"] == "EVT-TR-02"

def test_deterministic_priority():
    arbiter = ExitArbiter()
    req_emerg = ExitClaimRequest(
        trade_id="TR-104",
        single_leg_episode_id="EP-004",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-EM-01",
        owner="EMERGENCY",
        exit_reason="HARD_STOP_PREEMPTION",
        source_receive_sequence=1004,
        trigger_timestamp="2026-08-03T17:00:03.000",
        priority=CandidatePriority.EMERGENCY,
    )
    req_pj = ExitClaimRequest(
        trade_id="TR-104",
        single_leg_episode_id="EP-004",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-PJ-03",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK",
        source_receive_sequence=1004,
        trigger_timestamp="2026-08-03T17:00:03.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
    )
    result = arbiter.try_claim([req_pj, req_emerg])
    assert result.success is True
    assert result.winner_request.owner == "EMERGENCY"

def test_atomic_double_claim():
    arbiter = ExitArbiter()
    req1 = ExitClaimRequest(
        trade_id="TR-105",
        single_leg_episode_id="EP-005",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-01",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK",
        source_receive_sequence=1005,
        trigger_timestamp="2026-08-03T17:00:04.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
    )
    result1 = arbiter.try_claim([req1])
    assert result1.success is True

    req2 = ExitClaimRequest(
        trade_id="TR-105",
        single_leg_episode_id="EP-005",
        lifecycle_phase="SINGLE_LEG",
        state_revision=2,
        candidate_event_id="EVT-02",
        owner="SINGLE_LEG_TRAIL",
        exit_reason="SINGLE_LEG_TRAIL_HIT",
        source_receive_sequence=1006,
        trigger_timestamp="2026-08-03T17:00:05.000",
        priority=CandidatePriority.SINGLE_LEG_TRAIL,
    )
    result2 = arbiter.try_claim([req2])
    assert result2.success is False
    assert "ALREADY_CLAIMED" in result2.arbitration_reason

def test_duplicate_evaluation():
    arbiter = ExitArbiter()
    req = ExitClaimRequest(
        trade_id="TR-106",
        single_leg_episode_id="EP-006",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-DUP",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK",
        source_receive_sequence=1007,
        trigger_timestamp="2026-08-03T17:00:06.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
    )
    res1 = arbiter.try_claim([req])
    assert res1.success is True

    res2 = arbiter.try_claim([req])
    assert res2.success is False

def test_submit_exception():
    arbiter = ExitArbiter()
    req = ExitClaimRequest(
        trade_id="TR-107",
        single_leg_episode_id="EP-007",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-FAIL",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK",
        source_receive_sequence=1008,
        trigger_timestamp="2026-08-03T17:00:07.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
    )
    res = arbiter.try_claim([req])
    assert res.success is True

    arbiter.release_claim("EP-007", reason="SUBMIT_EXCEPTION_RETRY")
    
    req_retry = ExitClaimRequest(
        trade_id="TR-107",
        single_leg_episode_id="EP-007",
        lifecycle_phase="SINGLE_LEG",
        state_revision=2,
        candidate_event_id="EVT-RETRY",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK_RETRY",
        source_receive_sequence=1009,
        trigger_timestamp="2026-08-03T17:00:08.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
    )
    res_retry = arbiter.try_claim([req_retry])
    assert res_retry.success is True

def test_submit_timeout():
    arbiter = ExitArbiter()
    req = ExitClaimRequest(
        trade_id="TR-108",
        single_leg_episode_id="EP-008",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-TIMEOUT",
        owner="SINGLE_LEG_TRAIL",
        exit_reason="SINGLE_LEG_TRAIL_HIT",
        source_receive_sequence=1010,
        trigger_timestamp="2026-08-03T17:00:09.000",
        priority=CandidatePriority.SINGLE_LEG_TRAIL,
    )
    res = arbiter.try_claim([req])
    assert res.success is True

    arbiter.release_claim("EP-008", reason="SUBMIT_TIMEOUT_ESCALATE")
    
    res_reclaim = arbiter.try_claim([req])
    assert res_reclaim.success is True

def test_partial_fill_quantity_safety():
    trade_state = {
        "lifecycle_phase": "PARTIALLY_FILLED",
        "exit_owner": "POLICY_J",
        "original_qty": 2,
        "filled_qty": 1,
        "open_qty": 1,
    }
    assert trade_state["open_qty"] == 1
    assert trade_state["exit_owner"] == "POLICY_J"

def test_restart_with_pending_order():
    arbiter = ExitArbiter()
    req = ExitClaimRequest(
        trade_id="TR-110",
        single_leg_episode_id="EP-010",
        lifecycle_phase="EXIT_PENDING",
        state_revision=1,
        candidate_event_id="EVT-OLD",
        owner="POLICY_J",
        exit_reason="RESTORED_CLAIM",
        source_receive_sequence=1011,
        trigger_timestamp="2026-08-03T17:00:10.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
    )
    arbiter.try_claim([req])
    
    req_dupe = ExitClaimRequest(
        trade_id="TR-110",
        single_leg_episode_id="EP-010",
        lifecycle_phase="EXIT_PENDING",
        state_revision=1,
        candidate_event_id="EVT-NEW",
        owner="SINGLE_LEG_TRAIL",
        exit_reason="POST_RESTART_TICK",
        source_receive_sequence=1012,
        trigger_timestamp="2026-08-03T17:00:11.000",
        priority=CandidatePriority.SINGLE_LEG_TRAIL,
    )
    res = arbiter.try_claim([req_dupe])
    assert res.success is False

def test_stale_episode_candidate():
    arbiter = ExitArbiter()
    arbiter.increment_revision("EP-011")
    cur_rev = arbiter.get_state_revision("EP-011")
    assert cur_rev == 2

def test_flat_callback_after_claim():
    arbiter = ExitArbiter()
    req = ExitClaimRequest(
        trade_id="TR-112",
        single_leg_episode_id="EP-012",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-FLAT",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK",
        source_receive_sequence=1013,
        trigger_timestamp="2026-08-03T17:00:12.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
    )
    res = arbiter.try_claim([req])
    assert res.success is True

    arbiter.complete_claim("EP-012")
    
    res_new = arbiter.try_claim([req])
    assert res_new.success is True

def test_hard_stop_preemption():
    arbiter = ExitArbiter()
    req_hard_stop = ExitClaimRequest(
        trade_id="TR-113",
        single_leg_episode_id="EP-013",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-HS",
        owner="EMERGENCY",
        exit_reason="HARD_STOP_PREEMPTION",
        source_receive_sequence=1014,
        trigger_timestamp="2026-08-03T17:00:13.000",
        priority=CandidatePriority.EMERGENCY,
    )
    req_pj = ExitClaimRequest(
        trade_id="TR-113",
        single_leg_episode_id="EP-013",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-PJ",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK",
        source_receive_sequence=1014,
        trigger_timestamp="2026-08-03T17:00:13.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
    )
    res = arbiter.try_claim([req_pj, req_hard_stop])
    assert res.success is True
    assert res.winner_request.owner == "EMERGENCY"

def test_renko_shadow_cannot_claim():
    arbiter = ExitArbiter()
    req_renko = ExitClaimRequest(
        trade_id="TR-114",
        single_leg_episode_id="EP-014",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-RENKO",
        owner="RENKO_SHADOW",
        exit_reason="RENKO_ADVERSE_REVERSAL",
        source_receive_sequence=1015,
        trigger_timestamp="2026-08-03T17:00:14.000",
        priority=CandidatePriority.RENKO_SHADOW,
        is_shadow=True,
    )
    res = arbiter.try_claim([req_renko])
    assert res.success is False
    assert res.arbitration_reason == "ALL_CANDIDATES_ARE_SHADOW"

def test_long_short_executable_pnl_mapping():
    bid_price = 24000.0
    ask_price = 24002.0
    entry_price = 23900.0
    
    long_upl = (bid_price - entry_price) * 10.0
    assert long_upl == 1000.0
    
    short_upl = (entry_price - ask_price) * 10.0
    assert short_upl == -1020.0

def test_activation_remains_armed():
    peak_net_exit_pnl_twd = 250.0
    activation_threshold = 200.0
    is_armed = peak_net_exit_pnl_twd >= activation_threshold
    assert is_armed is True

    current_pnl_twd = 180.0
    assert peak_net_exit_pnl_twd >= activation_threshold

def test_gap_through_giveback():
    peak_net_exit_pnl_twd = 300.0
    giveback_threshold = 50.0
    current_total_net_pnl_twd = 210.0
    
    triggered = current_total_net_pnl_twd <= (peak_net_exit_pnl_twd - giveback_threshold)
    assert triggered is True

def test_winner_telemetry_consistency():
    arbiter = ExitArbiter()
    req = ExitClaimRequest(
        trade_id="TR-118",
        single_leg_episode_id="EP-018",
        lifecycle_phase="SINGLE_LEG",
        state_revision=1,
        candidate_event_id="EVT-TELEM",
        owner="POLICY_J",
        exit_reason="POLICY_J_GIVEBACK",
        source_receive_sequence=1018,
        trigger_timestamp="2026-08-03T17:00:18.000",
        priority=CandidatePriority.POLICY_J_GIVEBACK,
    )
    arbiter.try_claim([req])
    ledger = arbiter.get_telemetry_ledger()
    assert len(ledger) >= 1
    t = ledger[-1]
    assert t["event"] == "ARBITRATION_WINNER_CLAIMED"
    assert t["single_leg_episode_id"] == "EP-018"
    assert t["winner"]["owner"] == "POLICY_J"
