# Policy K: Feature Schema and Data Structures (V1.2.4)
# Purpose: Define dataclass schemas for Policy K Phase K1 Feature Logger & Outcome Builder
# Enforces: Manifest Versioning, Honest Tax Parity Semantics, Outcome Callbacks,
# Dual-Index Join Verification, and Detailed Health Counters.

import time
import json
import hashlib
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, Optional

POLICY_K_VERSION_MANIFEST = {
    'br_version': '1.2.4',
    'schema_version': '1.2.0',
    'feature_version': '1.2.0',
    'outcome_version': '1.2.4'
}

SCHEMA_VERSION = POLICY_K_VERSION_MANIFEST['schema_version']
FEATURE_VERSION = POLICY_K_VERSION_MANIFEST['feature_version']

@dataclass
class PolicyKFeatureSnapshot:
    snapshot_id: str
    event_key: str
    strategy_instance_id: str
    trading_date: str
    trade_incarnation_id: str
    trade_id: str
    release_event_id: str
    release_sequence_no: int
    
    producer_generation_id: str
    producer_pid: int
    producer_commit_sha: str
    
    event_time: str
    received_at: str
    processed_at: str
    release_decision_time: str
    
    latest_near_quote_event_time: str
    latest_far_quote_event_time: str
    return_1s_anchor_event_time: str
    return_3s_anchor_event_time: str
    return_5s_anchor_event_time: str
    return_15s_anchor_event_time: str
    return_window_end_event_time: str
    slope_window_start_time: str
    slope_window_end_time: str
    vol_window_start_time: str
    vol_window_end_time: str
    
    feature_source_max_event_time: str
    feature_source_min_event_time: str
    feature_window_observation_count: int
    return_5s_effective_lookback_ms: float
    
    release_leg: str
    remaining_leg: str
    remaining_position_side: str
    
    near_bid: float
    near_ask: float
    near_mid: float
    near_last: float
    far_bid: float
    far_ask: float
    far_mid: float
    far_last: float
    
    near_quote_age_ms: float
    far_quote_age_ms: float
    quote_timestamp_skew_ms: float
    
    remaining_return_1s: float
    remaining_return_3s: float
    remaining_return_5s: float
    remaining_return_15s: float
    
    near_return_1s: float
    near_return_5s: float
    near_return_15s: float
    far_return_1s: float
    far_return_5s: float
    far_return_15s: float
    spread_return_1s: float
    spread_return_5s: float
    spread_return_15s: float
    
    remaining_slope: float
    spread_velocity: float
    spread_acceleration: float
    z: float
    z_velocity: float
    z_acceleration: float
    
    near_bid_ask_width: float
    far_bid_ask_width: float
    near_update_count: int
    far_update_count: int
    directional_tick_ratio: float
    
    near_atr: float
    far_atr: float
    remaining_realized_volatility: float
    stop_distance_points: float
    stop_distance_sigma: float
    
    session: str
    seconds_from_session_open: float
    entry_holding_ms: float
    release_reason: str
    
    feature_complete: bool = True
    ineligibility_reasons: str = ''
    schema_version: str = SCHEMA_VERSION
    feature_version: str = FEATURE_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyKOutcomeSnapshot:
    trade_id: str
    release_event_id: str
    event_key: str
    
    actual_single_leg_net_pnl_twd: float
    combined_at_release_net_pnl_twd: float
    residual_hold_uplift_twd: float
    
    remaining_leg_exit_price: float
    remaining_leg_exit_time: str
    remaining_leg_exit_reason: str
    
    post_release_mae_points: float
    post_release_mfe_points: float
    time_to_mae_ms: float
    time_to_mfe_ms: float
    residual_holding_ms: float
    
    contract_code: str
    contract_spec_sha256: str
    account_fee_profile_id: str
    account_fee_profile_sha256: str
    
    market_data_basis: str = 'LIVE_MARKET'
    decision_basis: str = 'ACTUAL_RUNTIME'
    execution_basis: str = 'PAPER_SIMULATED'
    fee_basis: str = 'ESTIMATED'
    tax_calculation_mode: str = 'ESTIMATED'
    tax_rounding_target: str = 'BROKER_STATEMENT_PARITY'
    tax_parity_status: str = 'NOT_VERIFIED_IN_PAPER'
    broker_statement_parity_verified: bool = False
    
    near_position_side: str = 'LONG'
    near_exit_action: str = 'SELL'
    near_executable_price: float = 0.0
    near_executable_price_source: str = 'EXECUTABLE_BID'
    
    far_position_side: str = 'SHORT'
    far_exit_action: str = 'BUY'
    far_executable_price: float = 0.0
    far_executable_price_source: str = 'EXECUTABLE_ASK'
    
    combined_fee_twd: float = 20.0
    combined_slippage_twd: float = 0.0
    
    counterfactual_valid: bool = True
    outcome_complete: bool = True
    exclusion_reasons: str = ''
    outcome_version: str = POLICY_K_VERSION_MANIFEST['outcome_version']
    schema_version: str = SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class CaptureResult:
    status: str
    event_key: Optional[str] = None
    snapshot_id: Optional[str] = None
    failure_reason: str = ''

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyKHealthCounters:
    release_decision_count: int = 0
    eligible_release_count: int = 0
    feature_capture_attempt_count: int = 0
    feature_captured_count: int = 0
    duplicate_suppressed_count: int = 0
    provenance_invalid_count: int = 0
    feature_write_failed_count: int = 0
    
    outcome_expected_count: int = 0
    outcome_callback_count: int = 0
    outcome_captured_count: int = 0
    outcome_duplicate_suppressed_count: int = 0
    outcome_skipped_no_feature_count: int = 0
    orphan_outcome_count: int = 0
    outcome_write_failed_count: int = 0
    
    successful_join_count: int = 0
    orphan_feature_count: int = 0
    
    decision_influence_count: int = 0
    future_leakage_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class PolicyKLabeledEpisode:
    snapshot: PolicyKFeatureSnapshot
    outcome: PolicyKOutcomeSnapshot
    
    label_uplift_twd: float
    label_uplift_positive: bool
    label_large_uplift: bool
    economic_threshold_twd: float = 0.0
    sample_weight: float = 1.0
    dataset_split: str = 'train'

    def to_dict(self) -> Dict[str, Any]:
        return {
            'snapshot': self.snapshot.to_dict(),
            'outcome': self.outcome.to_dict(),
            'label_uplift_twd': self.label_uplift_twd,
            'label_uplift_positive': self.label_uplift_positive,
            'label_large_uplift': self.label_large_uplift,
            'economic_threshold_twd': self.economic_threshold_twd,
            'sample_weight': self.sample_weight,
            'dataset_split': self.dataset_split
        }
