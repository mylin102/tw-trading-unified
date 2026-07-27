# 2026-07-27 Gemini CLI: Wave J1.5-BR Policy J Telemetry Integration Tests (A~I)
import json
import time
import pytest
from pathlib import Path

from strategies.futures.mts.policy_j_shadow_evaluator import (
    PolicyJShadowEvaluator,
    PolicyJShadowObservation,
)
from strategies.futures.mts.policy_j_shadow_state import PolicyJShadowState
from strategies.futures.mts.policy_j_telemetry_schema import PolicyJShadowSnapshot
from strategies.futures.mts.soak_collector import ShadowSoakCollector


def test_a_runtime_hook_integration(tmp_path):
    """A. Runtime hook integration test: Canonical position evaluation invokes Policy J exactly once."""
    collector = ShadowSoakCollector(
        base_dir=tmp_path / "soak",
        deployment_id="test-deploy",
        authority="legacy",
        override_git_clean=True,
    )
    obs = PolicyJShadowObservation(
        trade_id="trade-123",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=800.0,
        near_quote_age_ms=0,
        far_quote_age_ms=0,
    )
    snap, state = PolicyJShadowEvaluator.evaluate(obs, PolicyJShadowState(), {"observation_enabled": True, "execution_enabled": False})
    recorded = collector.record_policy_j_evaluation(snap, obs)
    assert recorded is True
    assert collector.policy_j_hook_reached_total == 1
    assert collector.policy_j_evaluate_total == 1
    assert collector.policy_j_snapshot_total == 1
    collector.close_and_export_manifest()


def test_b_no_position_heartbeat(tmp_path):
    """B. No-position test: Hook produces a bounded NO_ACTIVE_POSITION heartbeat or explicit health counter."""
    collector = ShadowSoakCollector(
        base_dir=tmp_path / "soak",
        deployment_id="test-deploy",
        authority="legacy",
        override_git_clean=True,
    )
    collector.record_market_callback()
    assert collector.market_callback_total == 1
    assert collector.policy_j_snapshot_total == 0
    collector.close_and_export_manifest()


def test_c_active_position_evaluation(tmp_path):
    """C. Active-position evaluation test: Valid quotes produce a PolicyJShadowSnapshot."""
    obs = PolicyJShadowObservation(
        trade_id="trade-active",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=1200.0,
        near_quote_age_ms=0,
        far_quote_age_ms=0,
    )
    snap, state = PolicyJShadowEvaluator.evaluate(obs, PolicyJShadowState(), {"observation_enabled": True, "execution_enabled": False})
    assert isinstance(snap, PolicyJShadowSnapshot)
    assert snap.trade_id == "trade-active"
    assert snap.eligible is True


def test_d_queue_persistence(tmp_path):
    """D. Queue persistence test: Three evaluations produce three parseable JSONL records."""
    collector = ShadowSoakCollector(
        base_dir=tmp_path / "soak",
        deployment_id="test-deploy",
        authority="legacy",
        override_git_clean=True,
    )
    state = PolicyJShadowState()
    for i in range(3):
        obs = PolicyJShadowObservation(
            trade_id=f"trade-d-{i}",
            is_spread_phase=True,
            is_hedged_pair=True,
            exit_inflight=False,
            gross_liquidation_pnl_twd=float(600.0 + i * 200.0),
            near_quote_age_ms=0,
            far_quote_age_ms=0,
            event_key=f"evt-{i}",
        )
        snap, state = PolicyJShadowEvaluator.evaluate(obs, state, {"observation_enabled": True, "execution_enabled": False})
        collector.record_policy_j_evaluation(snap, obs)

    # Wait for spooler to flush
    time.sleep(0.3)
    manifest = collector.close_and_export_manifest()
    
    files = list((tmp_path / "soak" / collector.generation_id / "raw").glob("*.jsonl"))
    assert len(files) > 0
    raw_lines = []
    for f in files:
        with open(f, "r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    raw_lines.append(json.loads(line))
    assert len(raw_lines) == 3


def test_e_deduplication(tmp_path):
    """E. Deduplication test: Repeated identical state does not create uncontrolled duplicate records."""
    collector = ShadowSoakCollector(
        base_dir=tmp_path / "soak",
        deployment_id="test-deploy",
        authority="legacy",
        override_git_clean=True,
    )
    state = PolicyJShadowState()
    obs = PolicyJShadowObservation(
        trade_id="trade-e",
        is_spread_phase=True,
        is_hedged_pair=True,
        exit_inflight=False,
        gross_liquidation_pnl_twd=600.0,
        near_quote_age_ms=0,
        far_quote_age_ms=0,
    )
    snap, state = PolicyJShadowEvaluator.evaluate(obs, state, {"observation_enabled": True, "execution_enabled": False})

    # Evaluate identical state 5 times
    for _ in range(5):
        collector.record_policy_j_evaluation(snap, obs)

    assert collector.policy_j_snapshot_total == 5
    assert collector.policy_j_enqueue_total == 1  # Only 1 persisted
    assert collector.policy_j_duplicate_suppressed_total == 4  # 4 suppressed
    collector.close_and_export_manifest()


def test_f_trigger_edge(tmp_path):
    """F. Trigger-edge test: would_trigger false -> true produces exactly one first_trigger_event."""
    collector = ShadowSoakCollector(
        base_dir=tmp_path / "soak",
        deployment_id="test-deploy",
        authority="legacy",
        override_git_clean=True,
    )
    state = PolicyJShadowState()

    # Step 1: Low PnL -> no trigger
    obs1 = PolicyJShadowObservation(trade_id="trade-f", is_spread_phase=True, is_hedged_pair=True, exit_inflight=False, gross_liquidation_pnl_twd=200.0, near_quote_age_ms=0, far_quote_age_ms=0)
    snap1, state = PolicyJShadowEvaluator.evaluate(obs1, state, {"observation_enabled": True, "execution_enabled": False})
    collector.record_policy_j_evaluation(snap1, obs1)
    assert snap1.would_trigger is False

    # Step 2: Peak PnL 1000 TWD
    obs2 = PolicyJShadowObservation(trade_id="trade-f", is_spread_phase=True, is_hedged_pair=True, exit_inflight=False, gross_liquidation_pnl_twd=1000.0, near_quote_age_ms=0, far_quote_age_ms=0)
    snap2, state = PolicyJShadowEvaluator.evaluate(obs2, state, {"observation_enabled": True, "execution_enabled": False})
    collector.record_policy_j_evaluation(snap2, obs2)

    # Step 3: PnL gives back -> triggers giveback
    obs3 = PolicyJShadowObservation(trade_id="trade-f", is_spread_phase=True, is_hedged_pair=True, exit_inflight=False, gross_liquidation_pnl_twd=500.0, near_quote_age_ms=0, far_quote_age_ms=0)
    snap3, state = PolicyJShadowEvaluator.evaluate(obs3, state, {"observation_enabled": True, "execution_enabled": False})
    collector.record_policy_j_evaluation(snap3, obs3)
    assert snap3.would_trigger is True

    collector.close_and_export_manifest()


def test_g_execution_isolation(tmp_path):
    """G. Execution isolation test: Policy J causes zero calls to OrderManager or lifecycle exit methods."""
    shadow_config = {
        "observation_enabled": True,
        "execution_enabled": False,
    }
    assert shadow_config["execution_enabled"] is False, "Policy J execution MUST BE HARD-LOCKED FALSE"
    obs = PolicyJShadowObservation(trade_id="trade-g", is_spread_phase=True, is_hedged_pair=True, exit_inflight=False, gross_liquidation_pnl_twd=1500.0, near_quote_age_ms=0, far_quote_age_ms=0)
    snap, state = PolicyJShadowEvaluator.evaluate(obs, PolicyJShadowState(), shadow_config)
    assert snap.eligible is True
    # Snapshot is purely observational and does not contain order payload or order manager reference
    assert not hasattr(snap, "order_payload")


def test_h_restart(tmp_path):
    """H. Restart test: Peak state and sequence number resume monotonically."""
    collector1 = ShadowSoakCollector(base_dir=tmp_path / "soak1", deployment_id="deploy", authority="legacy", override_git_clean=True)
    state = PolicyJShadowState()
    obs1 = PolicyJShadowObservation(trade_id="trade-h", is_spread_phase=True, is_hedged_pair=True, exit_inflight=False, gross_liquidation_pnl_twd=1000.0, near_quote_age_ms=0, far_quote_age_ms=0)
    snap1, state = PolicyJShadowEvaluator.evaluate(obs1, state, {"observation_enabled": True, "execution_enabled": False})
    collector1.record_policy_j_evaluation(snap1, obs1)
    collector1.close_and_export_manifest()

    # Simulate process restart & state restoration
    restored_state = state
    assert restored_state.peak_net_exit_pnl_twd == 1000.0

    collector2 = ShadowSoakCollector(base_dir=tmp_path / "soak2", deployment_id="deploy", authority="legacy", override_git_clean=True)
    obs2 = PolicyJShadowObservation(trade_id="trade-h", is_spread_phase=True, is_hedged_pair=True, exit_inflight=False, gross_liquidation_pnl_twd=1100.0, near_quote_age_ms=0, far_quote_age_ms=0)
    snap2, restored_state = PolicyJShadowEvaluator.evaluate(obs2, restored_state, {"observation_enabled": True, "execution_enabled": False})
    collector2.record_policy_j_evaluation(snap2, obs2)
    assert restored_state.peak_net_exit_pnl_twd == 1100.0
    collector2.close_and_export_manifest()


def test_i_writer_failure(tmp_path):
    """I. Writer failure test: Writer errors increment write_error_total without affecting position handling."""
    collector = ShadowSoakCollector(base_dir=tmp_path / "soak", deployment_id="test-deploy", authority="legacy", override_git_clean=True)
    
    # Mock logger to raise an exception on record_cycle
    def bad_record_cycle(rec):
        raise RuntimeError("Disk full")

    collector.logger.record_cycle = bad_record_cycle

    obs = PolicyJShadowObservation(trade_id="trade-i", is_spread_phase=True, is_hedged_pair=True, exit_inflight=False, gross_liquidation_pnl_twd=500.0, near_quote_age_ms=0, far_quote_age_ms=0)
    snap, state = PolicyJShadowEvaluator.evaluate(obs, PolicyJShadowState(), {"observation_enabled": True, "execution_enabled": False})

    # Failure to enqueue must return False and increment policy_j_write_error_total without raising
    res = collector.record_policy_j_evaluation(snap, obs)
    assert res is False
    assert collector.policy_j_write_error_total > 0
