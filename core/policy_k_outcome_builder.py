# Policy K: Outcome Builder (Phase K1 Offline Episode Joiner - V1.2.3)
# Purpose: Offline join of PolicyKFeatureSnapshot and PolicyKOutcomeSnapshot
# Enforces: Manifest Versioning, Honest Tax Parity Semantics, Contract-Spec & Fee Profile Separation.

import hashlib
import json
import logging
from typing import Dict, Any, List, Optional
from core.policy_k_feature_schema import (
    PolicyKFeatureSnapshot,
    PolicyKOutcomeSnapshot,
    PolicyKLabeledEpisode,
    POLICY_K_VERSION_MANIFEST
)

logger = logging.getLogger('PolicyKOutcomeBuilder')

CONTRACT_SPEC_CATALOG = {
    'TMF': {
        'contract_code': 'TMF',
        'contract_name': 'Micro Taiwan Index Futures',
        'point_value_twd': 10.0,
        'tax_rate': 0.00002
    },
    'MTX': {
        'contract_code': 'MTX',
        'contract_name': 'Mini Taiwan Index Futures',
        'point_value_twd': 50.0,
        'tax_rate': 0.00002
    },
    'TXF': {
        'contract_code': 'TXF',
        'contract_name': 'Taiwan Index Futures (Large)',
        'point_value_twd': 200.0,
        'tax_rate': 0.00002
    }
}

ACCOUNT_FEE_PROFILE_CATALOG = {
    'SHIOAJI_PAPER_DEFAULT': {
        'account_profile_id': 'SHIOAJI_PAPER_DEFAULT',
        'broker': 'SHIOAJI',
        'commission_per_contract_per_side_twd': 10.0,
        'exchange_fee_per_contract_per_side_twd': 0.0,
        'rounding_rule': 'BROKER_STATEMENT_PARITY'
    }
}

def resolve_contract_spec(symbol_or_code: str = 'TMF') -> Dict[str, Any]:
    code = symbol_or_code.upper()
    if 'TMF' in code:
        return CONTRACT_SPEC_CATALOG['TMF']
    elif 'MTX' in code or 'MXF' in code:
        return CONTRACT_SPEC_CATALOG['MTX']
    elif 'TXF' in code or 'TX' in code:
        return CONTRACT_SPEC_CATALOG['TXF']
    return CONTRACT_SPEC_CATALOG['TMF']

def resolve_account_fee_profile(profile_id: str = 'SHIOAJI_PAPER_DEFAULT') -> Dict[str, Any]:
    return ACCOUNT_FEE_PROFILE_CATALOG.get(profile_id, ACCOUNT_FEE_PROFILE_CATALOG['SHIOAJI_PAPER_DEFAULT'])

def compute_canonical_sha256(payload: Dict[str, Any]) -> str:
    canonical_json = json.dumps(payload, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(canonical_json.encode('utf-8')).hexdigest()

class PolicyKOutcomeBuilder:
    def __init__(
        self,
        symbol_or_code: str = 'TMF',
        account_profile_id: str = 'SHIOAJI_PAPER_DEFAULT',
        economic_threshold_twd: float = 0.0
    ):
        self.symbol_or_code = symbol_or_code
        self.account_profile_id = account_profile_id
        self.economic_threshold_twd = economic_threshold_twd
        self.contract_spec = resolve_contract_spec(symbol_or_code)
        self.fee_profile = resolve_account_fee_profile(account_profile_id)

    def build_outcome_snapshot(
        self,
        trade_id: str,
        release_event_id: str,
        event_key: str,
        actual_single_leg_net_pnl_twd: float,
        combined_at_release_net_pnl_twd: float,
        remaining_leg_exit_price: float,
        remaining_leg_exit_time: str,
        remaining_leg_exit_reason: str,
        post_release_mae_points: float,
        post_release_mfe_points: float,
        time_to_mae_ms: float,
        time_to_mfe_ms: float,
        residual_holding_ms: float,
        near_position_side: str = 'LONG',
        far_position_side: str = 'SHORT',
        near_bid: float = 0.0,
        near_ask: float = 0.0,
        far_bid: float = 0.0,
        far_ask: float = 0.0,
        combined_fee_twd: float = 20.0,
        combined_slippage_twd: float = 0.0,
        execution_basis: str = 'PAPER_SIMULATED',
        fee_basis: str = 'ESTIMATED',
        exclusion_reasons: str = ''
    ) -> PolicyKOutcomeSnapshot:
        near_exit_action = 'SELL' if near_position_side == 'LONG' else 'BUY'
        near_exec_price = near_bid if near_exit_action == 'SELL' else near_ask
        near_exec_source = 'EXECUTABLE_BID' if near_exit_action == 'SELL' else 'EXECUTABLE_ASK'

        far_exit_action = 'SELL' if far_position_side == 'LONG' else 'BUY'
        far_exec_price = far_bid if far_exit_action == 'SELL' else far_ask
        far_exec_source = 'EXECUTABLE_BID' if far_exit_action == 'SELL' else 'EXECUTABLE_ASK'

        residual_hold_uplift_twd = actual_single_leg_net_pnl_twd - combined_at_release_net_pnl_twd
        counterfactual_valid = (near_exec_price > 0 and far_exec_price > 0 and exclusion_reasons == '')
        
        return PolicyKOutcomeSnapshot(
            trade_id=trade_id,
            release_event_id=release_event_id,
            event_key=event_key,
            actual_single_leg_net_pnl_twd=actual_single_leg_net_pnl_twd,
            combined_at_release_net_pnl_twd=combined_at_release_net_pnl_twd,
            residual_hold_uplift_twd=residual_hold_uplift_twd,
            remaining_leg_exit_price=remaining_leg_exit_price,
            remaining_leg_exit_time=remaining_leg_exit_time,
            remaining_leg_exit_reason=remaining_leg_exit_reason,
            post_release_mae_points=post_release_mae_points,
            post_release_mfe_points=post_release_mfe_points,
            time_to_mae_ms=time_to_mae_ms,
            time_to_mfe_ms=time_to_mfe_ms,
            residual_holding_ms=residual_holding_ms,
            contract_code=self.contract_spec['contract_code'],
            contract_spec_sha256=compute_canonical_sha256(self.contract_spec),
            account_fee_profile_id=self.fee_profile['account_profile_id'],
            account_fee_profile_sha256=compute_canonical_sha256(self.fee_profile),
            market_data_basis='LIVE_MARKET',
            decision_basis='ACTUAL_RUNTIME',
            execution_basis=execution_basis,
            fee_basis=fee_basis,
            tax_calculation_mode='ESTIMATED',
            tax_rounding_target='BROKER_STATEMENT_PARITY',
            tax_parity_status='NOT_VERIFIED_IN_PAPER',
            broker_statement_parity_verified=False,
            near_position_side=near_position_side,
            near_exit_action=near_exit_action,
            near_executable_price=near_exec_price,
            near_executable_price_source=near_exec_source,
            far_position_side=far_position_side,
            far_exit_action=far_exit_action,
            far_executable_price=far_exec_price,
            far_executable_price_source=far_exec_source,
            combined_fee_twd=combined_fee_twd,
            combined_slippage_twd=combined_slippage_twd,
            counterfactual_valid=counterfactual_valid,
            outcome_complete=counterfactual_valid,
            exclusion_reasons=exclusion_reasons,
            outcome_version=POLICY_K_VERSION_MANIFEST['outcome_version'],
            schema_version=POLICY_K_VERSION_MANIFEST['schema_version']
        )

    def join_episode(
        self,
        snapshot: PolicyKFeatureSnapshot,
        outcome: PolicyKOutcomeSnapshot,
        dataset_split: str = 'train'
    ) -> Optional[PolicyKLabeledEpisode]:
        if snapshot.event_key != outcome.event_key:
            logger.error(f'Mismatch joining event_key {snapshot.event_key} vs {outcome.event_key}')
            return None
            
        y_i = outcome.residual_hold_uplift_twd
        label_positive = (y_i > 0.0)
        label_large = (y_i > self.economic_threshold_twd)
        
        return PolicyKLabeledEpisode(
            snapshot=snapshot,
            outcome=outcome,
            label_uplift_twd=y_i,
            label_uplift_positive=label_positive,
            label_large_uplift=label_large,
            economic_threshold_twd=self.economic_threshold_twd,
            sample_weight=1.0,
            dataset_split=dataset_split
        )
