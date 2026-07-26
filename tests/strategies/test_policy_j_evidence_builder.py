# 2026-07-26 Gemini CLI: Unit tests for PolicyJEvidenceBuilder
import pytest

from strategies.futures.mts.counterfactual_evidence_schema import ExclusionReason, FillModel
from strategies.futures.mts.policy_j_evidence_builder import PolicyJEvidenceBuilder


def test_evidence_builder_long_near_short_far_triggered():
    # 1. LONG-near / SHORT-far triggered trade
    shadow_snaps = [
        {
            "trade_id": "TRADE_LONG_001",
            "session_date": "20260726",
            "event_time": "2026-07-26T09:00:00",
            "eligibility_reason": "HEDGED_PAIR_SPREAD",
            "estimated_net_exit_pnl_twd": 450.0,
            "activation_net_pnl_twd": 300.0,
            "giveback_twd": 100.0,
            "first_trigger_event": True,
            "config_hash": "HASH123",
            "near_executable_price": 22000.0,
            "far_executable_price": 22050.0,
        }
    ]
    outcomes = [
        {
            "trade_id": "TRADE_LONG_001",
            "session_date": "20260726",
            "session": "DAY",
            "direction": "BUY_NEAR_SELL_FAR",
            "entry_time": "2026-07-26T09:00:00",
            "actual_final_net_pnl_twd": 200.0,
            "actual_mfe_net_pnl_twd": 500.0,
        }
    ]

    builder = PolicyJEvidenceBuilder(fill_model=FillModel.EXECUTABLE.value)
    facts, manifest = builder.build_evidence(shadow_snaps, outcomes)

    assert len(facts) == 1
    tf = facts[0]
    assert tf.trade_id == "TRADE_LONG_001"
    assert tf.eligible_for_analysis is True
    assert tf.exclusion_reason == ExclusionReason.NONE.value
    assert tf.first_trigger_time == "2026-07-26T09:00:00"
    assert tf.hypothetical_net_exit_pnl_twd == 450.0
    assert tf.actual_final_net_pnl_twd == 200.0
    assert tf.delta_net_pnl_twd == 250.0             # 450 - 200
    assert tf.ped_actual_twd == 300.0                # 500 - 200
    assert tf.ped_policy_j_twd == 50.0               # 500 - 450
    assert tf.ped_improvement_twd == 250.0           # 300 - 50

    # Fill pricing check: SELL near (22000 - 1 = 21999), BUY far (22051 + 1 = 22052)
    assert tf.hypothetical_exit_price_near == 21999.0
    assert tf.hypothetical_exit_price_far == 22052.0


def test_evidence_builder_short_near_long_far_triggered():
    # 2. SHORT-near / LONG-far triggered trade
    shadow_snaps = [
        {
            "trade_id": "TRADE_SHORT_002",
            "session_date": "20260726",
            "event_time": "2026-07-26T09:10:00",
            "eligibility_reason": "HEDGED_PAIR_SPREAD",
            "estimated_net_exit_pnl_twd": 380.0,
            "activation_net_pnl_twd": 300.0,
            "giveback_twd": 100.0,
            "first_trigger_event": True,
            "config_hash": "HASH456",
            "near_executable_price": 22000.0,
            "far_executable_price": 22050.0,
        }
    ]
    outcomes = [
        {
            "trade_id": "TRADE_SHORT_002",
            "session_date": "20260726",
            "session": "NIGHT",
            "direction": "SELL_NEAR_BUY_FAR",
            "entry_time": "2026-07-26T09:10:00",
            "actual_final_net_pnl_twd": 150.0,
            "actual_mfe_net_pnl_twd": 400.0,
        }
    ]

    builder = PolicyJEvidenceBuilder(fill_model=FillModel.EXECUTABLE.value)
    facts, manifest = builder.build_evidence(shadow_snaps, outcomes)

    tf = facts[0]
    assert tf.direction == "SELL_NEAR_BUY_FAR"
    # Fill pricing check: BUY near (22001 + 1 = 22002), SELL far (22050 - 1 = 22049)
    assert tf.hypothetical_exit_price_near == 22002.0
    assert tf.hypothetical_exit_price_far == 22049.0


def test_evidence_builder_eligible_untriggered():
    # 3. Eligible but untriggered trade
    shadow_snaps = [
        {
            "trade_id": "TRADE_UNTRIG_003",
            "session_date": "20260726",
            "event_time": "2026-07-26T10:00:00",
            "eligibility_reason": "HEDGED_PAIR_SPREAD",
            "estimated_net_exit_pnl_twd": 120.0,  # Below activation threshold
            "activation_net_pnl_twd": 300.0,
            "giveback_twd": 100.0,
            "first_trigger_event": False,
            "config_hash": "HASH789",
        }
    ]
    outcomes = [
        {
            "trade_id": "TRADE_UNTRIG_003",
            "session_date": "20260726",
            "session": "DAY",
            "direction": "BUY_NEAR_SELL_FAR",
            "entry_time": "2026-07-26T10:00:00",
            "actual_final_net_pnl_twd": 180.0,
            "actual_mfe_net_pnl_twd": 220.0,
        }
    ]

    builder = PolicyJEvidenceBuilder(fill_model=FillModel.EXECUTABLE.value)
    facts, manifest = builder.build_evidence(shadow_snaps, outcomes)

    tf = facts[0]
    assert tf.eligible_for_analysis is True
    assert tf.exclusion_reason == ExclusionReason.NONE.value
    assert tf.first_trigger_time is None
    assert tf.hypothetical_net_exit_pnl_twd is None
    assert tf.delta_net_pnl_twd is None
    assert tf.actual_final_net_pnl_twd == 180.0


def test_evidence_builder_excluded_trade():
    # 4. Excluded trade due to stale quote
    shadow_snaps = [
        {
            "trade_id": "TRADE_STALE_004",
            "session_date": "20260726",
            "event_time": "2026-07-26T11:00:00",
            "eligibility_reason": "NEAR_QUOTE_STALE",
            "estimated_net_exit_pnl_twd": None,
        }
    ]
    outcomes = [
        {
            "trade_id": "TRADE_STALE_004",
            "session_date": "20260726",
            "session": "DAY",
            "direction": "BUY_NEAR_SELL_FAR",
            "entry_time": "2026-07-26T11:00:00",
            "actual_final_net_pnl_twd": -50.0,
        }
    ]

    builder = PolicyJEvidenceBuilder(fill_model=FillModel.EXECUTABLE.value)
    facts, manifest = builder.build_evidence(shadow_snaps, outcomes)

    tf = facts[0]
    assert tf.eligible_for_analysis is False
    assert tf.exclusion_reason == ExclusionReason.QUOTE_STALE.value


def test_evidence_builder_manifest_and_determinism():
    # 5 & 6. Manifest metrics and reproduction hash determinism
    builder = PolicyJEvidenceBuilder(fill_model=FillModel.EXECUTABLE.value)
    facts1, m1 = builder.build_evidence([], [])
    facts2, m2 = builder.build_evidence([], [])

    assert m1.reproduction_hash == m2.reproduction_hash
    assert m1.source_trade_count == 0
    assert m1.eligible_trade_count == 0
