"""Pre-breach state clone — canonical (replay + A4 share this contract).

clone_from_state(event_stream, breach_replay_seq, state_snapshot):
- deep-copies the snapshot (actual source mutation NEVER affects the clone)
- adds a canonical hash over the ordered clone payload
- reads ONLY events with replay_seq <= breach_replay_seq — breach/release
  FUTURE events are never read (the caller passes the bounded stream slice)
- incomplete input (missing snapshot / schema fields) -> typed
  ("NOT_AVAILABLE", [exact missing fields])
"""

import copy
import hashlib
import json

CLONE_SCHEMA_FIELDS = {
    "positions", "policy_peak", "durable_candidate", "warmup", "armed",
    "atr", "reference_prices", "pending_orders", "quote_freshness",
    "controller", "lifecycle", "release", "trail", "cooldown",
    "config_version",
}

SCHEMA_VERSION = "phase-transition-v1"


def clone_schema_version():
    """Schema version + the full field set (field-name keys)."""
    return {"version": SCHEMA_VERSION,
            **{f: "state" for f in sorted(CLONE_SCHEMA_FIELDS)}}


def _canonical_hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def clone_from_state(event_stream, breach_replay_seq, state_snapshot,
                     schema_fields=None):
    """Value-complete pre-breach clone.

    - state_snapshot missing/empty -> NOT_AVAILABLE
    - required schema fields (default: replay schema; adapters may pass
      their own field set) absent from the snapshot -> NOT_AVAILABLE
      naming the exact missing fields
    - otherwise a deep copy + canonical hash (immutable under source
      mutation)
    - the clone point is STRICTLY BEFORE the breach: only events with
      replay_seq < breach_replay_seq may be referenced — the breach event
      itself and all release/future events are never read
    """
    fields = schema_fields or CLONE_SCHEMA_FIELDS
    if not state_snapshot:
        return ("NOT_AVAILABLE", ["state_snapshot"])
    missing = sorted(fields - set(state_snapshot))
    if missing:
        return ("NOT_AVAILABLE", missing)
    clone = copy.deepcopy(dict(state_snapshot))
    # the bounded stream prefix is the only event evidence the clone may
    # rely on; the breach event and release/future events are excluded by
    # construction (strictly-before semantics)
    bounded = [e for e in (event_stream or [])
               if e.get("replay_seq", 0) < (breach_replay_seq or 0)]
    clone["_breach_replay_seq"] = breach_replay_seq
    clone["_stream_prefix_hash"] = _canonical_hash(bounded)
    clone["_canonical_hash"] = _canonical_hash(clone)
    return clone


def clone_state_at_decision(strategy_state, decision_ts):
    """Compatibility wrapper: pre-breach clone at a decision timestamp.

    Kept for callers that snapshot directly (no event stream); deep copy +
    canonical hash. A None strategy_state -> typed NOT_AVAILABLE.
    """
    if strategy_state is None:
        return ("NOT_AVAILABLE", ["strategy_state"])
    clone = copy.deepcopy(dict(strategy_state))
    clone["_clone_ts"] = decision_ts
    clone["_canonical_hash"] = _canonical_hash(clone)
    return clone
