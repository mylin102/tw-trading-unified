# Policy K: Unit Tests for Zero Future Leakage, Manifest Versioning & Outcome Callback (V1.2.4)
import os
import json
import tempfile
from core.policy_k_feature_schema import (
    PolicyKFeatureSnapshot,
    PolicyKOutcomeSnapshot,
    CaptureResult,
    PolicyKHealthCounters,
    POLICY_K_VERSION_MANIFEST
)
from core.policy_k_feature_collector import PolicyKFeatureCollector
from core.policy_k_outcome_builder import (
    PolicyKOutcomeBuilder,
    resolve_contract_spec,
    resolve_account_fee_profile,
    compute_canonical_sha256
)

def test_manifest_versioning():
    assert POLICY_K_VERSION_MANIFEST['br_version'] == '1.2.4'
    assert POLICY_K_VERSION_MANIFEST['schema_version'] == '1.2.0'
    assert POLICY_K_VERSION_MANIFEST['feature_version'] == '1.2.0'
    assert POLICY_K_VERSION_MANIFEST['outcome_version'] == '1.2.4'
    print('[PASS] test_manifest_versioning')


def test_cross_restart_idempotency_p0_fix():
    with tempfile.TemporaryDirectory() as tmpdir:
        storage_file = os.path.join(tmpdir, 'snapshots.jsonl')
        
        collector1 = PolicyKFeatureCollector(feature_storage_path=storage_file)
        release_time = '2026-07-29T09:30:00.000000'
        near_quote = {'bid_price': 41000.0, 'ask_price': 41002.0, 'price': 41001.0, 'quote_age_ms': 5.0, 'timestamp': release_time}
        far_quote = {'bid_price': 41180.0, 'ask_price': 41183.0, 'price': 41181.5, 'quote_age_ms': 8.0, 'timestamp': release_time}
        
        res1 = collector1.capture_snapshot(
            strategy_instance_id='strat-01',
            trading_date='20260729',
            trade_incarnation_id='inc-999',
            trade_id='trade-test-001',
            release_event_id='rel-001',
            release_sequence_no=1,
            release_decision_time=release_time,
            release_leg='FAR',
            remaining_leg='NEAR',
            remaining_position_side='LONG',
            near_quote=near_quote,
            far_quote=far_quote,
            producer_generation_id='gen-001'
        )
        assert res1.status == 'CAPTURED'
        
        collector2 = PolicyKFeatureCollector(feature_storage_path=storage_file)
        res2 = collector2.capture_snapshot(
            strategy_instance_id='strat-01',
            trading_date='20260729',
            trade_incarnation_id='inc-999',
            trade_id='trade-test-001',
            release_event_id='rel-001',
            release_sequence_no=1,
            release_decision_time=release_time,
            release_leg='FAR',
            remaining_leg='NEAR',
            remaining_position_side='LONG',
            near_quote=near_quote,
            far_quote=far_quote,
            producer_generation_id='gen-002'
        )
        
        assert res2.status == 'DUPLICATE_SUPPRESSED'
        print('[PASS] test_cross_restart_idempotency_p0_fix')


def test_outcome_callback_and_health_counters_in_bundle():
    with tempfile.TemporaryDirectory() as tmpdir:
        snap_file = os.path.join(tmpdir, 'snapshots.jsonl')
        out_file = os.path.join(tmpdir, 'outcomes.jsonl')
        
        collector = PolicyKFeatureCollector(feature_storage_path=snap_file, outcome_storage_path=out_file)
        release_time = '2026-07-29T09:30:00.000000'
        near_quote = {'bid_price': 41000.0, 'ask_price': 41002.0, 'price': 41001.0, 'timestamp': release_time}
        far_quote = {'bid_price': 41180.0, 'ask_price': 41183.0, 'price': 41181.5, 'timestamp': release_time}
        
        res = collector.capture_snapshot(
            strategy_instance_id='strat-01',
            trading_date='20260729',
            trade_incarnation_id='inc-999',
            trade_id='trade-test-001',
            release_event_id='rel-001',
            release_sequence_no=1,
            release_decision_time=release_time,
            release_leg='FAR',
            remaining_leg='NEAR',
            remaining_position_side='LONG',
            near_quote=near_quote,
            far_quote=far_quote
        )
        assert res.status == 'CAPTURED'
        
        builder = PolicyKOutcomeBuilder(symbol_or_code='TMF')
        outcome = builder.build_outcome_snapshot(
            trade_id='trade-test-001',
            release_event_id='rel-001',
            event_key=res.event_key,
            actual_single_leg_net_pnl_twd=-500.0,
            combined_at_release_net_pnl_twd=-200.0,
            remaining_leg_exit_price=41050.0,
            remaining_leg_exit_time='2026-07-29T09:35:00',
            remaining_leg_exit_reason='TRAIL_STOP',
            post_release_mae_points=120.0,
            post_release_mfe_points=10.0,
            time_to_mae_ms=15000.0,
            time_to_mfe_ms=2000.0,
            residual_holding_ms=300000.0,
            near_position_side='LONG',
            far_position_side='SHORT',
            near_bid=41000.0,
            near_ask=41002.0,
            far_bid=41180.0,
            far_ask=41183.0
        )
        
        res_outcome = collector.capture_outcome(outcome)
        assert res_outcome.status == 'CAPTURED'
        assert collector.health_counters.feature_captured_count == 1
        assert collector.health_counters.outcome_captured_count == 1
        assert collector.health_counters.successful_join_count == 1
        assert outcome.tax_calculation_mode == 'ESTIMATED'
        assert outcome.tax_parity_status == 'NOT_VERIFIED_IN_PAPER'
        assert outcome.broker_statement_parity_verified is False
        print('[PASS] test_outcome_callback_and_health_counters_in_bundle')

def test_dual_index_join_verification():
    with tempfile.TemporaryDirectory() as tmpdir:
        snap_file = os.path.join(tmpdir, 'snapshots.jsonl')
        out_file = os.path.join(tmpdir, 'outcomes.jsonl')
        collector = PolicyKFeatureCollector(feature_storage_path=snap_file, outcome_storage_path=out_file)
        
        # 1.Orphan Outcome (A tried outcome without feature snapshot in index)
        builder = PolicyKOutcomeBuilder(symbol_or_code='TMF')
        outcome_orphan = builder.build_outcome_snapshot(
            trade_id='trade-orphan-001',
            release_event_id='rel-orphan-001',
            event_key='strat-01_20260729_no_feature_key_1_FAR',
            actual_single_leg_net_pnl_twd=-500.0,
            combined_at_release_net_pnl_twd=-200.0,
            remaining_leg_exit_price=41050.0,
            remaining_leg_exit_time='2026-07-29T09:35:00',
            remaining_leg_exit_reason='TRAIL_STOP',
            post_release_mae_points=120.0,
            post_release_mfe_points=10.0,
            time_to_mae_ms=15000.0,
            time_to_mfe_ms=2000.0,
            residual_holding_ms=300000.0
        )
        res_orphan = collector.capture_outcome(outcome_orphan)
        assert res_orphan.status == 'PROVENANCE_INVALID'
        assert collector.health_counters.orphan_outcome_count == 1
        assert collector.health_counters.successful_join_count == 0
        print('[PASS] test_dual_index_join_verification')


if __name__ == '__main__':
    test_manifest_versioning()
    test_cross_restart_idempotency_p0_fix()
    test_outcome_callback_and_health_counters_in_bundle()
    test_dual_index_join_verification()
    print('All Policy K BR V2.4 unit tests passed successfully!')
