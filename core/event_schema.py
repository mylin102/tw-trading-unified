"""Event field registry — the one place that says what each event carries.

WHY: the event ledger is both the audit trail and the reconstruction source.
When one event name is emitted with different fields depending on the code
path, consumers read a missing key as "no value" instead of "this emitter
never provided it" — the same defect class as float(_age or 0.0).

CONTRACT:
  * EVENT_FIELDS[event] is the union of every field any emitter supplies.
  * normalize() fills the missing ones with None, so every record has the
    full shape. It never removes a field and never changes a value.
  * An event name that is not registered here is passed through unchanged
    and counted (undeclared_events) so a new emitter is visible rather than
    silently unfixed.

GENERATED from the call sites on 2026-11-11; regenerate with
scripts/ci/check_event_schema.py --emit-registry and review the diff.
"""
from __future__ import annotations

import threading
from datetime import datetime
from collections import Counter

EVENT_FIELDS: dict[str, frozenset[str]] = {
    "BIDASK_DROPPED": frozenset({"code", "count", "reason"}),
    "BIDASK_RECOVERY_EXHAUSTED": frozenset({"action"}),
    "BIDASK_RESUBSCRIBE_ATTEMPT": frozenset({"attempt"}),
    "BIDASK_RESUBSCRIBE_FAILED": frozenset({"attempt", "error_type"}),
    "BIDASK_STREAM_STALE": frozenset({"ages_seconds", "count"}),
    "COMBINED_EXIT_0905_CANCELLED": frozenset({"combined_exit_group_id", "leg_role", "order_id", "trade_id"}),
    "COMBINED_EXIT_COMPLETED": frozenset({"far_fill_price", "group_id", "near_fill_price", "trade_id"}),
    "COMBO_ORDER_BROKER_CONFIRMED": frozenset({"broker_order_id", "order_id", "status", "trade_id"}),
    "COMBO_ORDER_BROKER_QUERY_UNAVAILABLE": frozenset({"order_id", "reason", "trade_id"}),
    "COMBO_ORDER_INTENT_CREATED": frozenset({"combo_legs", "entry_offset_ticks", "far_bbo_state", "far_quote_age_ms", "intent_id", "max_entry_offset_ticks", "near_bbo_state", "near_quote_age_ms", "order_id", "price", "qty", "ref_ohlc", "reference_spread", "side", "strategy", "symbol", "tick_size", "trade_id", "type"}),
    "COMBO_ORDER_RECONCILIATION_PENDING": frozenset({"broker_order_id", "order_id", "reason", "status", "trade_id"}),
    "COMBO_ORDER_REJECTED": frozenset({"order_id", "reason", "trade_id"}),
    "COMBO_ORDER_SUBMITTED": frozenset({"combo_legs", "entry_offset_ticks", "far_bbo_state", "far_quote_age_ms", "max_entry_offset_ticks", "near_bbo_state", "near_quote_age_ms", "order_id", "price", "reference_spread", "strategy", "tick_size", "trade_id"}),
    "COMBO_PARTIAL_FILL_RECOVERY": frozenset({"recovery_scope", "trade_id"}),
    "COMBO_STATE_CLEAR_FAILED": frozenset({"error_type", "reason", "trade_id"}),
    "CONTRACT_MISMATCH_BLOCKED": frozenset({"held_far", "held_near", "order_far", "order_near", "trade_id"}),
    "ENTRY_AUDIT": frozenset({"action", "atr", "decision", "entry_z", "event_time", "expected_reversion", "far_contract", "far_delivery_date", "far_price", "far_price_source", "far_side", "far_tick_age_ms", "near_contract", "near_delivery_date", "near_price", "near_price_source", "near_side", "near_tick_age_ms", "reason", "signal_event_id", "spread_ma", "spread_now", "spread_std", "spread_z", "trade_id"}),
    "ENTRY_BLOCKED_STALE_QUOTE": frozenset({"action", "far_age_ms", "far_source", "near_age_ms", "near_source", "reason", "threshold"}),
    "ENTRY_SUBMITTED": frozenset({"action", "combo_legs", "far_entry", "far_side", "intent_id", "near_entry", "near_side", "order_id", "spread_z", "trade_id"}),
    "EXIT_ONLY_BBO_OBSERVED": frozenset({"bbo_hash", "bbo_payload", "reconciliation_id"}),
    "EXIT_ONLY_FLAT_RECONCILED": frozenset({}),
    "EXIT_ONLY_HYDRATION_BLOCKED": frozenset({"reason"}),
    "EXIT_ONLY_HYDRATION_FAILED": frozenset({"reason"}),
    "EXIT_ONLY_POSITION_HYDRATED": frozenset({"legs", "reconciliation_id", "snapshot_hash", "trade_id"}),
    "EXIT_ONLY_QUARANTINED": frozenset({"action", "bbo_input_v2", "reason", "trade_id"}),
    "EXIT_ONLY_RECONCILE_FAILED": frozenset({"reason", "reconciliation_id"}),
    "EXIT_ONLY_RENEWAL_FAILED": frozenset({"reason", "renewal_provenance"}),
    "EXIT_ONLY_TERMINAL_AMBIGUITY": frozenset({"reconciliation_id"}),
    "FDEAL_CALLBACK": frozenset({"callback_exception_count", "fdeal_count", "forder_count", "last_fdeal_callback_at", "last_order_callback_at", "normalization_result", "observation_type", "observed_at", "rejection_reason", "snapshot_generation", "source_status"}),
    "FDEAL_CALLBACK_REJECTED": frozenset({"broker_order_id", "code", "normalization_result", "observation_type", "ordno", "raw_price", "raw_quantity", "rejection_reason", "seqno"}),
    "FILL_REJECTED_MISSING_BROKER_IDENTITY": frozenset({"reason"}),
    "FILL_REJECTED_REMAINING": frozenset({"order_id", "reason"}),
    "FORDER_CALLBACK": frozenset({"callback_exception_count", "fdeal_count", "forder_count", "last_fdeal_callback_at", "last_order_callback_at", "normalization_result", "observation_type", "observed_at", "rejection_reason", "snapshot_generation", "source_status"}),
    "HELD_CONTRACTS_RESTORED": frozenset({"far_contract", "near_contract", "source", "trade_id"}),
    "LEG_FILLED": frozenset({"order_id", "price", "qty", "ref_ohlc", "slippage", "symbol"}),
    "LEG_LOCK_PAIR_PARTIAL": frozenset({"contract", "reason", "trade_id"}),
    "LEG_LOCK_PARTIAL_FILL": frozenset({"contract", "filled_qty", "qty", "reason", "trade_id"}),
    "LEG_LOCK_QUERY_FAILED": frozenset({"contract", "reason", "trade_id"}),
    "LEG_LOCK_QUERY_NO_TERMINAL": frozenset({"contract", "reason", "trade_id"}),
    "LEG_LOCK_REBIND_AMBIGUOUS": frozenset({"contract", "reason", "trade_id"}),
    "LEG_LOCK_RECONCILE_COMMIT_FAILED": frozenset({"contract", "detail", "reason"}),
    "LEG_LOCK_RECONCILE_LOCK_WRITE_FAILED": frozenset({"contract", "detail", "reason"}),
    "LEG_LOCK_RECONCILE_MANIFEST_FAILED": frozenset({"contract", "detail", "reason"}),
    "LEG_LOCK_RETIRED_BY_SNAPSHOT": frozenset({"broker_order_id", "contract", "reason", "trade_id"}),
    "MTS_ACTIVE_PAIR_SNAPSHOT": frozenset({"canonical_far_aliases", "canonical_near_aliases", "far_contract_code", "far_delivery_date", "near_contract_code", "near_delivery_date", "resolver_ready_monotonic_ns", "session_identity", "subscription_generation"}),
    "MTS_ENTRY_PARTIAL_SUBMISSION": frozenset({"failed_order_id", "reason", "submitted_broker_order_id", "submitted_order_id", "trade_id"}),
    "MTS_EVALUATOR_LAG": frozenset({"clock", "first_eval", "lag_secs", "last_eval_at", "slo_secs", "trade_id"}),
    "OPERATOR_ATTESTATION": frozenset({}),
    "ORDER_BLOCKED_PENDING_EXISTS": frozenset({"contract", "reason", "trade_id"}),
    "ORDER_CALLBACK_EXCEPTION": frozenset({"normalization_result", "observation_type", "rejection_reason"}),
    "ORDER_CALLBACK_REGISTERED": frozenset({"normalization_result", "observation_type", "registration_generation"}),
    "ORDER_CALLBACK_REGISTRATION_FAILED": frozenset({"normalization_result", "observation_type", "registration_generation", "rejection_reason"}),
    "ORDER_INTENT_BLOCKED": frozenset({"action", "audit", "bbo_input_v2", "contract", "error", "leg", "premise", "reason", "trade_id"}),
    "ORDER_INTENT_CREATED": frozenset({"order_id", "price", "qty", "ref_ohlc", "side", "strategy", "symbol", "trade_id", "type"}),
    "ORDER_REJECTED_LOCAL": frozenset({"entry_offset_ticks", "error", "error_type", "far_bbo_state", "far_quote_age_ms", "max_entry_offset_ticks", "near_bbo_state", "near_quote_age_ms", "order_id", "price", "qty", "reason", "ref_ohlc", "reference_spread", "side", "strategy", "symbol", "tick_size", "trade_id", "type"}),
    "ORDER_SUBMITTED": frozenset({"combined_exit_group_id", "contract", "event", "event_id", "exit_reason", "exit_stage", "leg_role", "order_id", "price", "qty", "reason_source", "ref_ohlc", "release_reason", "side", "strategy", "symbol", "trade_id", "type", "winner"}),
    "ORDER_SUBMIT_FAILED": frozenset({"action", "contract", "error_type", "reason", "strategy", "trade_id"}),
    "ORDER_TIMEOUT": frozenset({"order_id"}),
    "PAPER_EXIT_INTENT_RECLAIMED": frozenset({"intent_id", "legs", "reason", "trade_id"}),
    "PAPER_EXIT_INTENT_RECONCILE_FAILED": frozenset({"error_type", "intent_id", "leg", "reason", "trade_id"}),
    "PARTIAL_SUBMISSION_QUARANTINE": frozenset({"contract", "reason", "trade_id"}),
    "POLICY_J_SINGLE_LEG_TRIGGERED": frozenset({}),
    "POSITION_CONTRACT_UNAVAILABLE": frozenset({"action", "reason", "trade_id"}),
    "RECONCILIATION_FAILURE": frozenset({"broker_pos", "mem_pos", "reason", "released"}),
    "REFRESH_CHANNEL_STATE": frozenset({"normalization_result", "observation_type", "state"}),
    "REFRESH_COMPLETED": frozenset({"account_type", "elapsed_ms", "normalized_count", "raw_count", "rejected_count", "rejection_reasons", "snapshot_generation", "status_distribution", "trades_count"}),
    "REFRESH_EXCEPTION": frozenset({"account_type", "callback_exception_count", "callback_registration_generation", "elapsed_ms", "error", "fdeal_count", "forder_count", "last_fdeal_callback_at", "last_order_callback_at", "normalized_count", "raw_count", "rejected_count", "rejection_reasons", "snapshot_generation", "status_distribution", "trades_count"}),
    "REFRESH_SKIPPED_DISCONNECTED": frozenset({"account_type", "callback_exception_count", "callback_registration_generation", "elapsed_ms", "error", "fdeal_count", "forder_count", "last_fdeal_callback_at", "last_order_callback_at", "normalized_count", "raw_count", "rejected_count", "rejection_reasons", "snapshot_generation", "status_distribution", "trades_count"}),
    "REFRESH_SKIPPED_INFLIGHT": frozenset({"account_type", "callback_exception_count", "callback_registration_generation", "elapsed_ms", "error", "fdeal_count", "forder_count", "last_fdeal_callback_at", "last_order_callback_at", "normalized_count", "raw_count", "rejected_count", "rejection_reasons", "snapshot_generation", "status_distribution", "trades_count"}),
    "REFRESH_STARTED": frozenset({"account_type", "callback_exception_count", "callback_registration_generation", "elapsed_ms", "error", "fdeal_count", "forder_count", "last_fdeal_callback_at", "last_order_callback_at", "normalized_count", "raw_count", "rejected_count", "rejection_reasons", "snapshot_generation", "status_distribution", "trades_count"}),
    "REFRESH_SUCCEEDED": frozenset({"account_type", "callback_exception_count", "callback_registration_generation", "elapsed_ms", "error", "fdeal_count", "forder_count", "last_fdeal_callback_at", "last_order_callback_at", "normalized_count", "raw_count", "rejected_count", "rejection_reasons", "snapshot_generation", "status_distribution", "trades_count"}),
    "REFRESH_TIMEOUT_UNCERTAIN": frozenset({"account_type", "callback_exception_count", "callback_registration_generation", "elapsed_ms", "error", "fdeal_count", "forder_count", "last_fdeal_callback_at", "last_order_callback_at", "normalized_count", "raw_count", "rejected_count", "rejection_reasons", "snapshot_generation", "status_distribution", "trades_count"}),
    "RELEASE_CONDITION_MET": frozenset({"release_stop", "signal", "trade_id"}),
    "RELEASE_EVAL_SKIP_NO_LOCAL_POSITION": frozenset({"broker_legs", "local_far_qty", "local_near_qty"}),
    "RELEASE_FAR_SUBMITTED": frozenset({"order_id", "price", "qty", "release_reason", "released_leg", "side", "strategy", "symbol", "trade_id", "type"}),
    "RELEASE_NEAR_SUBMITTED": frozenset({"order_id", "price", "qty", "release_reason", "released_leg", "side", "strategy", "symbol", "trade_id", "type"}),
    "SESSION_RECONNECT_FAILED": frozenset({"error_type", "source"}),
    "SESSION_RECONNECT_READY": frozenset({}),
    "SESSION_RECONNECT_TRIGGERED": frozenset({}),
    "SETTLEMENT_GATE_UNRESOLVED": frozenset({"reason", "trade_id"}),
    "WATCHDOG_ALERT": frozenset({"broker_position", "elapsed_secs", "lifecycle", "local_position", "pending_order_id", "reason"}),
    "WATCHDOG_BROKER_HAS_POSITION_OR_ORDER": frozenset({"broker_order_id", "order_id", "reason", "symbol"}),
    "WATCHDOG_BROKER_NO_POSITION_OR_ORDER": frozenset({"order_id", "reason"}),
    "WATCHDOG_BROKER_QUERY_UNAVAILABLE": frozenset({"order_id", "reason"}),}

undeclared_events: Counter[str] = Counter()
undeclared_fields: Counter[str] = Counter()

RESERVED_KEYS = frozenset({"event", "ts"})


def normalize(event: str, fields: dict) -> dict:
    """Return `fields` with every declared key present (missing -> None).

    Additive by construction: unknown keys are kept as-is, known keys
    missing from `fields` are added as None. Nothing is ever dropped, so a
    consumer that reads a key always finds it.
    """
    declared = EVENT_FIELDS.get(event)
    if declared is None:
        undeclared_events[event] += 1
        return fields
    extra = set(fields) - declared
    if extra:
        # The registry is the contract: a field nobody declared must not enter
        # the ledger, or the shape drifts again through this very writer.
        undeclared_fields[event] += len(extra)
    # RESERVED: "event" and "ts" are the envelope, written by the caller before
    # this runs. A call site that passes `event=` as a payload field
    # (ORDER_SUBMITTED does) would otherwise make this return {"event": None},
    # and event.update() would erase the event name -- observed on 2026-11-11
    # as two records with "event": null.
    allowed = declared
    if event in THROTTLED_EVENTS:
        allowed = declared | THROTTLE_FIELDS
    return {key: fields.get(key) for key in allowed
            if key not in RESERVED_KEYS}


# ── Emission throttling ──────────────────────────────────────────────────
# A standing condition is not a discrete action. HVWAP_RELEASE_BLOCKED was
# emitted 9,581 times in 13.5 minutes for ONE trade on 2026-11-11 -- 48% of the
# day's ledger -- because the same unchanged state was re-asserted every tick.
# Any analysis that counts events ("how often was a release blocked?") then
# measures the tick rate, not the phenomenon.
#
# Only state-assertion events are listed here. Discrete actions (orders,
# fills, settlements, failures) are NEVER throttled: three ORDER_SUBMITTED
# events mean three submissions, and suppressing one would be a lie.
THROTTLED_EVENTS = frozenset({
    "HVWAP_RELEASE_BLOCKED",
    "ENTRY_BLOCKED_STALE_QUOTE",
    "POLICY_J_GUARD_QUARANTINE",
})

THROTTLE_FIELDS = frozenset({"suppressed_count", "first_ts", "last_ts"})
THROTTLE_WINDOW_S = 60.0

_throttle_state: dict = {}
_throttle_lock = threading.Lock()


def _throttle_key(event: str, fields: dict) -> tuple:
    return (event, fields.get("trade_id"), fields.get("leg"),
            fields.get("reason"))


def throttle(event: str, fields: dict, now: float) -> tuple[bool, dict]:
    """Decide whether this emission is written, and what to add to it.

    Returns (emit, extra). The FIRST occurrence of a standing condition is
    always written immediately, so a crash can never erase the fact that it
    happened -- only the suppressed counter is lost. Repeats inside the window
    are counted; when the window rolls over, one record is written carrying
    suppressed_count/first_ts/last_ts.
    """
    if event not in THROTTLED_EVENTS:
        return True, {}
    key = _throttle_key(event, fields)
    ts = datetime.now().isoformat()
    with _throttle_lock:
        state = _throttle_state.get(key)
        if state is None:
            _throttle_state[key] = {"last": now, "count": 0, "first_ts": ts}
            return True, {}
        if now - state["last"] < THROTTLE_WINDOW_S:
            state["count"] += 1
            return False, {}
        extra = {"suppressed_count": state["count"], "first_ts": state["first_ts"],
                 "last_ts": ts}
        _throttle_state[key] = {"last": now, "count": 0, "first_ts": ts}
        return True, extra


def reset_throttle() -> None:
    """Test seam."""
    with _throttle_lock:
        _throttle_state.clear()
