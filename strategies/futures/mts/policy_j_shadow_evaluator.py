# 2026-07-26 Gemini CLI: Wave J1.5-B Pure Policy J Shadow Evaluator
from dataclasses import dataclass
from typing import Any, Tuple

from strategies.futures.mts.policy_j_shadow_state import PolicyJShadowState
from strategies.futures.mts.policy_j_telemetry_schema import (
    EligibilityReason,
    PolicyJShadowSignal,
    PolicyJShadowSnapshot,
)
from strategies.futures.mts.quote_coherence import (
    QuoteCoherenceInput,
    QuoteCoherenceReason,
    evaluate_quote_coherence,
)


@dataclass(frozen=True)
class PolicyJShadowObservation:
    """Pure observation input context passed to PolicyJShadowEvaluator."""
    trade_id: str | None
    is_spread_phase: bool
    is_hedged_pair: bool
    exit_inflight: bool
    gross_liquidation_pnl_twd: float | None
    commission_twd: float = 0.0
    exchange_fee_twd: float = 0.0
    tax_twd: float = 0.0
    bid_ask_cost_twd: float = 0.0
    near_quote_age_ms: int | None = None
    far_quote_age_ms: int | None = None
    max_quote_age_ms: int = 1000
    event_time: str = ""
    processed_at: str = ""
    config_hash: str = ""
    event_key: str | None = None


class PolicyJShadowEvaluator:
    """
    Pure evaluation engine for Policy J Shadow Mode.
    100% deterministic, 0 side effects, 0 I/O, 0 OrderManager references.
    """

    @staticmethod
    def evaluate(
        obs: PolicyJShadowObservation,
        state: PolicyJShadowState,
        config: dict[str, Any],
    ) -> Tuple[PolicyJShadowSnapshot, PolicyJShadowState]:
        """
        Execute deterministic 13-step Policy J shadow evaluation sequence.
        """
        # Ensure state corresponds to active trade_id (resets sequence_no to 0 if trade_id changed)
        current_state = state.with_trade(obs.trade_id)

        # Gate 0.1: Idempotency Check (Duplicate Event Key)
        event_key = obs.event_key or (f"{obs.trade_id}_{obs.event_time}" if obs.trade_id and obs.event_time else None)
        if event_key and event_key == current_state.last_event_key:
            # Retain previous sequence_no and state without bumping or re-evaluating
            snapshot = PolicyJShadowSnapshot(
                sequence_no=current_state.sequence_no,
                trade_id=obs.trade_id,
                event_time=obs.event_time,
                processed_at=obs.processed_at,
                mode="SHADOW_ONLY",
                eligible=current_state.armed,
                eligibility_reason=EligibilityReason.HEDGED_PAIR_SPREAD.value if current_state.armed else EligibilityReason.NOT_SPREAD_PHASE.value,
                peak_net_exit_pnl_twd=current_state.peak_net_exit_pnl_twd,
                would_trigger=current_state.would_trigger_emitted,
                execution_blocked=True,
                config_hash=obs.config_hash,
                shadow_signal=PolicyJShadowSignal.WOULD_EXIT_BOTH.value if current_state.would_trigger_emitted else PolicyJShadowSignal.MONITORING.value,
                first_trigger_event=False,
            )
            return snapshot, current_state

        next_seq = current_state.sequence_no + 1

        activation_twd = float(config.get("activation_net_pnl_twd", 300.0))
        giveback_twd = float(config.get("giveback_twd", 100.0))
        shadow_enabled = bool(config.get("shadow_enabled", True))

        # Gate 0: Policy Shadow Disabled
        if not shadow_enabled:
            snapshot = PolicyJShadowSnapshot(
                sequence_no=next_seq,
                trade_id=obs.trade_id,
                event_time=obs.event_time,
                processed_at=obs.processed_at,
                mode="SHADOW_ONLY",
                eligible=False,
                eligibility_reason=EligibilityReason.POLICY_DISABLED.value,
                activation_net_pnl_twd=activation_twd,
                giveback_twd=giveback_twd,
                would_trigger=False,
                execution_blocked=True,
                near_quote_age_ms=obs.near_quote_age_ms,
                far_quote_age_ms=obs.far_quote_age_ms,
                config_hash=obs.config_hash,
                shadow_signal=PolicyJShadowSignal.NO_SIGNAL.value,
            )
            new_state = PolicyJShadowState(
                trade_id=current_state.trade_id,
                peak_net_exit_pnl_twd=current_state.peak_net_exit_pnl_twd,
                sequence_no=next_seq,
                armed=current_state.armed,
                would_trigger_emitted=current_state.would_trigger_emitted,
                last_event_key=current_state.last_event_key,
            )
            return snapshot, new_state

        # Gate 1: Position Phase Eligibility (Must be SPREAD phase)
        if not obs.is_spread_phase:
            snapshot = PolicyJShadowSnapshot(
                sequence_no=next_seq,
                trade_id=obs.trade_id,
                event_time=obs.event_time,
                processed_at=obs.processed_at,
                mode="SHADOW_ONLY",
                eligible=False,
                eligibility_reason=EligibilityReason.NOT_SPREAD_PHASE.value,
                activation_net_pnl_twd=activation_twd,
                giveback_twd=giveback_twd,
                would_trigger=False,
                execution_blocked=True,
                near_quote_age_ms=obs.near_quote_age_ms,
                far_quote_age_ms=obs.far_quote_age_ms,
                config_hash=obs.config_hash,
                shadow_signal=PolicyJShadowSignal.NO_SIGNAL.value,
            )
            new_state = PolicyJShadowState(
                trade_id=current_state.trade_id,
                peak_net_exit_pnl_twd=current_state.peak_net_exit_pnl_twd,
                sequence_no=next_seq,
                armed=current_state.armed,
                would_trigger_emitted=current_state.would_trigger_emitted,
                last_event_key=current_state.last_event_key,
            )
            return snapshot, new_state

        # Gate 2: Hedged Pair Completeness (Must be dual leg)
        if not obs.is_hedged_pair:
            snapshot = PolicyJShadowSnapshot(
                sequence_no=next_seq,
                trade_id=obs.trade_id,
                event_time=obs.event_time,
                processed_at=obs.processed_at,
                mode="SHADOW_ONLY",
                eligible=False,
                eligibility_reason=EligibilityReason.SINGLE_LEG_ONLY.value,
                activation_net_pnl_twd=activation_twd,
                giveback_twd=giveback_twd,
                would_trigger=False,
                execution_blocked=True,
                near_quote_age_ms=obs.near_quote_age_ms,
                far_quote_age_ms=obs.far_quote_age_ms,
                config_hash=obs.config_hash,
                shadow_signal=PolicyJShadowSignal.NO_SIGNAL.value,
            )
            new_state = PolicyJShadowState(
                trade_id=current_state.trade_id,
                peak_net_exit_pnl_twd=current_state.peak_net_exit_pnl_twd,
                sequence_no=next_seq,
                armed=current_state.armed,
                would_trigger_emitted=current_state.would_trigger_emitted,
                last_event_key=current_state.last_event_key,
            )
            return snapshot, new_state

        # Gate 3: Exit Inflight Check
        if obs.exit_inflight:
            snapshot = PolicyJShadowSnapshot(
                sequence_no=next_seq,
                trade_id=obs.trade_id,
                event_time=obs.event_time,
                processed_at=obs.processed_at,
                mode="SHADOW_ONLY",
                eligible=False,
                eligibility_reason=EligibilityReason.EXIT_INFLIGHT.value,
                activation_net_pnl_twd=activation_twd,
                giveback_twd=giveback_twd,
                would_trigger=False,
                execution_blocked=True,
                near_quote_age_ms=obs.near_quote_age_ms,
                far_quote_age_ms=obs.far_quote_age_ms,
                config_hash=obs.config_hash,
                shadow_signal=PolicyJShadowSignal.NO_SIGNAL.value,
            )
            new_state = PolicyJShadowState(
                trade_id=current_state.trade_id,
                peak_net_exit_pnl_twd=current_state.peak_net_exit_pnl_twd,
                sequence_no=next_seq,
                armed=current_state.armed,
                would_trigger_emitted=current_state.would_trigger_emitted,
                last_event_key=current_state.last_event_key,
            )
            return snapshot, new_state

        # Gate 4: Quote Freshness & Coherence (shared contract with production evaluator)
        _qc_input = QuoteCoherenceInput(
            near_quote_age_ms=obs.near_quote_age_ms,
            far_quote_age_ms=obs.far_quote_age_ms,
            max_quote_age_ms=obs.max_quote_age_ms,
            has_exit_inflight=obs.exit_inflight,
            gross_pnl=obs.gross_liquidation_pnl_twd,
        )
        _qc = evaluate_quote_coherence(_qc_input)

        if not _qc.fresh or not _qc.coherent:
            if _qc.reason not in (QuoteCoherenceReason.READY.value,):
                snapshot = PolicyJShadowSnapshot(
                    sequence_no=next_seq,
                    trade_id=obs.trade_id,
                    event_time=obs.event_time,
                    processed_at=obs.processed_at,
                    mode="SHADOW_ONLY",
                    eligible=False,
                    eligibility_reason=_qc.reason,
                    activation_net_pnl_twd=activation_twd,
                    giveback_twd=giveback_twd,
                    would_trigger=False,
                    execution_blocked=True,
                    near_quote_age_ms=obs.near_quote_age_ms,
                    far_quote_age_ms=obs.far_quote_age_ms,
                    config_hash=obs.config_hash,
                    shadow_signal=PolicyJShadowSignal.NO_SIGNAL.value,
                )
                new_state = PolicyJShadowState(
                    trade_id=current_state.trade_id,
                    peak_net_exit_pnl_twd=current_state.peak_net_exit_pnl_twd,
                    sequence_no=next_seq,
                    armed=current_state.armed,
                    would_trigger_emitted=current_state.would_trigger_emitted,
                    last_event_key=current_state.last_event_key,
                )
                return snapshot, new_state

        # Step 5 & 6 & 7: Liquidation Valuation, Friction, Net Exit PnL
        gross = obs.gross_liquidation_pnl_twd if obs.gross_liquidation_pnl_twd is not None else 0.0
        friction = obs.commission_twd + obs.exchange_fee_twd + obs.tax_twd + obs.bid_ask_cost_twd
        net_exit_pnl = gross - friction

        # Step 8: Peak Update (Only when eligible & fresh)
        prev_peak = current_state.peak_net_exit_pnl_twd
        new_peak = net_exit_pnl if prev_peak is None else max(prev_peak, net_exit_pnl)

        # Step 9: Activation Evaluation
        is_armed = (new_peak >= activation_twd)

        # Step 10 & 11: Giveback & Shadow Signal
        would_trigger = False
        signal = PolicyJShadowSignal.MONITORING.value

        if is_armed:
            signal = PolicyJShadowSignal.ARMED.value
            drawdown = new_peak - net_exit_pnl
            if drawdown >= giveback_twd:
                would_trigger = True
                signal = PolicyJShadowSignal.WOULD_EXIT_BOTH.value

        # Emitted status tracking
        is_first_trigger = would_trigger and not current_state.would_trigger_emitted
        already_emitted = current_state.would_trigger_emitted or would_trigger

        snapshot = PolicyJShadowSnapshot(
            sequence_no=next_seq,
            trade_id=obs.trade_id,
            event_time=obs.event_time,
            processed_at=obs.processed_at,
            mode="SHADOW_ONLY",
            eligible=True,
            eligibility_reason=EligibilityReason.HEDGED_PAIR_SPREAD.value,
            gross_liquidation_pnl_twd=gross,
            estimated_friction_twd=friction,
            estimated_net_exit_pnl_twd=net_exit_pnl,
            peak_net_exit_pnl_twd=new_peak,
            activation_net_pnl_twd=activation_twd,
            giveback_twd=giveback_twd,
            would_trigger=would_trigger,
            execution_blocked=True,
            near_quote_age_ms=obs.near_quote_age_ms,
            far_quote_age_ms=obs.far_quote_age_ms,
            config_hash=obs.config_hash,
            shadow_signal=signal,
            first_trigger_event=is_first_trigger,
        )

        new_state = PolicyJShadowState(
            trade_id=current_state.trade_id,
            peak_net_exit_pnl_twd=new_peak,
            sequence_no=next_seq,
            armed=is_armed,
            would_trigger_emitted=already_emitted,
            last_event_key=event_key or f"{obs.trade_id}_{next_seq}",
        )

        return snapshot, new_state
