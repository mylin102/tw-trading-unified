# Policy K: Feature Collector & Outcome Logger (Phase K1 Logger - V1.2.3 Bundle)
import os
import time
import json
import logging
from typing import Dict, Any, Optional, Set
from core.policy_k_feature_schema import (
    PolicyKFeatureSnapshot,
    PolicyKOutcomeSnapshot,
    CaptureResult,
    PolicyKHealthCounters,
    SCHEMA_VERSION,
    FEATURE_VERSION,
    POLICY_K_VERSION_MANIFEST
)

logger = logging.getLogger('PolicyKFeatureCollector')

class PolicyKFeatureCollector:
    def __init__(
        self,
        feature_storage_path: str = 'logs/policy_k_snapshots.jsonl',
        outcome_storage_path: str = 'logs/policy_k_outcomes.jsonl'
    ):
        self.feature_storage_path = feature_storage_path
        self.outcome_storage_path = outcome_storage_path
        self.sequence_no = 0
        self._persisted_feature_event_keys: Set[str] = set()
        self._persisted_outcome_event_keys: Set[str] = set()
        self.health_counters = PolicyKHealthCounters()
        self._recover_dual_idempotency_index()
        
    def _recover_dual_idempotency_index(self):
        if os.path.exists(self.feature_storage_path):
            try:
                with open(self.feature_storage_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                rec = json.loads(line)
                                key = rec.get('event_key')
                                if key:
                                    self._persisted_feature_event_keys.add(key)
                                self.sequence_no = max(self.sequence_no, rec.get('release_sequence_no', 0))
                            except json.JSONDecodeError:
                                logger.warning('Ignoring partial or corrupt JSONL line during recovery')
                logger.info(f'Recovered {self.sequence_no} snapshots and {len(self._persisted_feature_event_keys)} feature keys.')
            except Exception as e:
                logger.warning(f'Failed to recover feature idempotency index: {e}')

        if os.path.exists(self.outcome_storage_path):
            try:
                with open(self.outcome_storage_path, 'r', encoding='utf-8') as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                rec = json.loads(line)
                                key = rec.get('event_key')
                                if key:
                                    self._persisted_outcome_event_keys.add(key)
                            except json.JSONDecodeError:
                                logger.warning('Ignoring partial or corrupt Outcome JSONL line during recovery')
                logger.info(f'Recovered {len(self._persisted_outcome_event_keys)} outcome keys.')
            except Exception as e:
                logger.warning(f'Failed to recover outcome idempotency index: {e}')

    def capture_snapshot(
        self,
        strategy_instance_id: str,
        trading_date: str,
        trade_incarnation_id: str,
        trade_id: str,
        release_event_id: str,
        release_sequence_no: int,
        release_decision_time: str,
        release_leg: str,
        remaining_leg: str,
        remaining_position_side: str,
        near_quote: Dict[str, Any],
        far_quote: Dict[str, Any],
        context: Dict[str, Any] = None,
        producer_generation_id: str = 'gen-001',
        producer_pid: int = 0,
        producer_commit_sha: str = 'dev'
    ) -> CaptureResult:
        self.health_counters.release_decision_count += 1
        self.health_counters.eligible_release_count += 1
        self.health_counters.feature_capture_attempt_count += 1
        
        event_key = f'{strategy_instance_id}_{trading_date}_{trade_incarnation_id}_{release_sequence_no}_{release_leg}'
        try:
            if event_key in self._persisted_feature_event_keys:
                self.health_counters.duplicate_suppressed_count += 1
                logger.info('Skipping duplicate release event key: %s', event_key)
                return CaptureResult(status='DUPLICATE_SUPPRESSED', event_key=event_key)
                
            self.sequence_no += 1
            now_str = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime())
            context = context or {}
            
            near_bid = float(near_quote.get('bid_price', near_quote.get('price', 0.0)))
            near_ask = float(near_quote.get('ask_price', near_quote.get('price', 0.0)))
            near_mid = (near_bid + near_ask) / 2.0 if (near_bid > 0 and near_ask > 0) else float(near_quote.get('price', 0.0))
            near_last = float(near_quote.get('price', 0.0))
            
            far_bid = float(far_quote.get('bid_price', far_quote.get('price', 0.0)))
            far_ask = float(far_quote.get('ask_price', far_quote.get('price', 0.0)))
            far_mid = (far_bid + far_ask) / 2.0 if (far_bid > 0 and far_ask > 0) else float(far_quote.get('price', 0.0))
            far_last = float(far_quote.get('price', 0.0))
            
            near_qage = float(near_quote.get('quote_age_ms', 0.0))
            far_qage = float(far_quote.get('quote_age_ms', 0.0))
            skew_ms = abs(near_qage - far_qage)
            
            near_quote_ts = near_quote.get('timestamp', release_decision_time)
            far_quote_ts = far_quote.get('timestamp', release_decision_time)
            ret_1s_anchor = context.get('return_1s_anchor_event_time', release_decision_time)
            ret_3s_anchor = context.get('return_3s_anchor_event_time', release_decision_time)
            ret_5s_anchor = context.get('return_5s_anchor_event_time', release_decision_time)
            ret_15s_anchor = context.get('return_15s_anchor_event_time', release_decision_time)
            ret_window_end = context.get('return_window_end_event_time', release_decision_time)
            slope_start = context.get('slope_window_start_time', release_decision_time)
            slope_end = context.get('slope_window_end_time', release_decision_time)
            vol_start = context.get('vol_window_start_time', release_decision_time)
            vol_end = context.get('vol_window_end_time', release_decision_time)
            
            max_ts = max(near_quote_ts, far_quote_ts, ret_window_end, slope_end, vol_end)
            min_ts = min(near_quote_ts, far_quote_ts, ret_15s_anchor, slope_start, vol_start)
            
            if max_ts > release_decision_time:
                self.health_counters.provenance_invalid_count += 1
                self.health_counters.future_leakage_count += 1
                logger.error('Future leakage detected: max_ts %s > release_decision_time %s', max_ts, release_decision_time)
                return CaptureResult(status='PROVENANCE_INVALID', event_key=event_key, failure_reason='FUTURE_LEAKAGE_DETECTED')
                
            snapshot_id = f'pkk-snap-{self.sequence_no:06d}-{release_event_id}'
            snapshot = PolicyKFeatureSnapshot(
                snapshot_id=snapshot_id,
                event_key=event_key,
                strategy_instance_id=strategy_instance_id,
                trading_date=trading_date,
                trade_incarnation_id=trade_incarnation_id,
                trade_id=trade_id,
                release_event_id=release_event_id,
                release_sequence_no=release_sequence_no,
                producer_generation_id=producer_generation_id,
                producer_pid=producer_pid,
                producer_commit_sha=producer_commit_sha,
                event_time=release_decision_time,
                received_at=now_str,
                processed_at=now_str,
                release_decision_time=release_decision_time,
                latest_near_quote_event_time=near_quote_ts,
                latest_far_quote_event_time=far_quote_ts,
                return_1s_anchor_event_time=ret_1s_anchor,
                return_3s_anchor_event_time=ret_3s_anchor,
                return_5s_anchor_event_time=ret_5s_anchor,
                return_15s_anchor_event_time=ret_15s_anchor,
                return_window_end_event_time=ret_window_end,
                slope_window_start_time=slope_start,
                slope_window_end_time=slope_end,
                vol_window_start_time=vol_start,
                vol_window_end_time=vol_end,
                feature_source_max_event_time=max_ts,
                feature_source_min_event_time=min_ts,
                feature_window_observation_count=int(context.get('observation_count', 20)),
                return_5s_effective_lookback_ms=float(context.get('return_5s_effective_lookback_ms', 5000.0)),
                release_leg=release_leg,
                remaining_leg=remaining_leg,
                remaining_position_side=remaining_position_side,
                near_bid=near_bid,
                near_ask=near_ask,
                near_mid=near_mid,
                near_last=near_last,
                far_bid=far_bid,
                far_ask=far_ask,
                far_mid=far_mid,
                far_last=far_last,
                near_quote_age_ms=near_qage,
                far_quote_age_ms=far_qage,
                quote_timestamp_skew_ms=skew_ms,
                remaining_return_1s=float(context.get('remaining_return_1s', 0.0)),
                remaining_return_3s=float(context.get('remaining_return_3s', 0.0)),
                remaining_return_5s=float(context.get('remaining_return_5s', 0.0)),
                remaining_return_15s=float(context.get('remaining_return_15s', 0.0)),
                near_return_1s=float(context.get('near_return_1s', 0.0)),
                near_return_5s=float(context.get('near_return_5s', 0.0)),
                near_return_15s=float(context.get('near_return_15s', 0.0)),
                far_return_1s=float(context.get('far_return_1s', 0.0)),
                far_return_5s=float(context.get('far_return_5s', 0.0)),
                far_return_15s=float(context.get('far_return_15s', 0.0)),
                spread_return_1s=float(context.get('spread_return_1s', 0.0)),
                spread_return_5s=float(context.get('spread_return_5s', 0.0)),
                spread_return_15s=float(context.get('spread_return_15s', 0.0)),
                remaining_slope=float(context.get('remaining_slope', 0.0)),
                spread_velocity=float(context.get('spread_velocity', 0.0)),
                spread_acceleration=float(context.get('spread_acceleration', 0.0)),
                z=float(context.get('z', 0.0)),
                z_velocity=float(context.get('z_velocity', 0.0)),
                z_acceleration=float(context.get('z_acceleration', 0.0)),
                near_bid_ask_width=abs(near_ask - near_bid) if (near_ask > 0 and near_bid > 0) else 0.0,
                far_bid_ask_width=abs(far_ask - far_bid) if (far_ask > 0 and far_ask > 0) else 0.0,
                near_update_count=int(context.get('near_update_count', 0)),
                far_update_count=int(context.get('far_update_count', 0)),
                directional_tick_ratio=float(context.get('directional_tick_ratio', 0.5)),
                near_atr=float(context.get('near_atr', 0.0)),
                far_atr=float(context.get('far_atr', 0.0)),
                remaining_realized_volatility=float(context.get('remaining_realized_volatility', 0.0)),
                stop_distance_points=float(context.get('stop_distance_points', 0.0)),
                stop_distance_sigma=float(context.get('stop_distance_sigma', 0.0)),
                session=context.get('session', 'day'),
                seconds_from_session_open=float(context.get('seconds_from_session_open', 0.0)),
                entry_holding_ms=float(context.get('entry_holding_ms', 0.0)),
                release_reason=context.get('release_reason', 'STOP_LOSS')
            )
            
            self._write_snapshot(snapshot)
            self._persisted_feature_event_keys.add(event_key)
            self.health_counters.feature_captured_count += 1
            self.health_counters.outcome_expected_count += 1
            return CaptureResult(status='CAPTURED', event_key=event_key, snapshot_id=snapshot_id)
        except Exception as e:
            self.health_counters.feature_write_failed_count += 1
            logger.exception('Fail-Open Catch in capture_snapshot: %s', e)
            return CaptureResult(status='WRITE_FAILED', event_key=event_key, failure_reason=str(e))

    def capture_outcome(self, outcome: PolicyKOutcomeSnapshot) -> CaptureResult:
        self.health_counters.outcome_callback_count += 1
        event_key = outcome.event_key
        
        if not event_key:
            self.health_counters.outcome_skipped_no_feature_count += 1
            return CaptureResult(status='PROVENANCE_INVALID', event_key=None, failure_reason='Missing feature event_key')
            
        if event_key not in self._persisted_feature_event_keys:
            self.health_counters.orphan_outcome_count += 1
            return CaptureResult(status='PROVENANCE_INVALID', event_key=event_key, failure_reason='Feature snapshot not found in index')
            
        if event_key in self._persisted_outcome_event_keys:
            self.health_counters.outcome_duplicate_suppressed_count += 1
            logger.info('Skipping duplicate outcome for event key: %s', event_key)
            return CaptureResult(status='DUPLICATE_SUPPRESSED', event_key=event_key)
            
        try:
            self._write_outcome(outcome)
            self._persisted_outcome_event_keys.add(event_key)
            self.health_counters.outcome_captured_count += 1
            self.health_counters.successful_join_count += 1
            return CaptureResult(status='CAPTURED', event_key=event_key)
        except Exception as e:
            self.health_counters.outcome_write_failed_count += 1
            logger.exception('Fail-Open Catch in capture_outcome: %s', e)
            return CaptureResult(status='WRITE_FAILED', event_key=event_key, failure_reason=str(e))

    def _write_outcome(self, outcome: PolicyKOutcomeSnapshot):
        with open(self.outcome_storage_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(outcome.to_dict()) + chr(10))

    def _write_snapshot(self, snapshot: PolicyKFeatureSnapshot):
        with open(self.feature_storage_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(snapshot.to_dict()) + chr(10))

    def _write_outcome(self, outcome: PolicyKOutcomeSnapshot):
        with open(self.outcome_storage_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(outcome.to_dict()) + chr(10))
