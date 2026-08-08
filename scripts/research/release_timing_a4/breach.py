"""Threshold breach snapshot + pre-breach clone — A4 engine.

Delegates the clone to the canonical replay contract pattern
(clone_from_state): deep-copy + canonical hash; reads ONLY the stream
prefix at/before breach_replay_seq — breach/release FUTURE events are
never consulted; incomplete input -> typed NOT_AVAILABLE naming the
exact missing fields.
"""

import copy
import hashlib
import json

CLONE_SCHEMA_FIELDS = {
    "positions", "policy_peak", "guard_warmup", "guard_armed",
    "atr", "reference_prices", "pending_candidates", "pending_orders",
    "quote_freshness", "controller", "lifecycle", "cooldown",
    "strategy_generation", "config_version",
}

SCHEMA_VERSION = "a4-v1"


def _canonical_hash(payload):
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def clone_schema_version():
    """Schema version + the full A4 field set (field-name keys)."""
    return {"version": SCHEMA_VERSION,
            **{f: "state" for f in sorted(CLONE_SCHEMA_FIELDS)}}


def clone_from_state(event_stream, breach_replay_seq, state_snapshot):
    """Value-complete pre-breach clone (canonical contract).

    - missing/empty snapshot -> ("NOT_AVAILABLE", ["state_snapshot"])
    - any required schema field absent -> ("NOT_AVAILABLE", [missing])
    - otherwise DEEP copy + canonical hash; only events with
      replay_seq <= breach_replay_seq may be referenced (future breach/
      release events are never read).
    """
    if not state_snapshot:
        return ("NOT_AVAILABLE", ["state_snapshot"])
    missing = sorted(CLONE_SCHEMA_FIELDS - set(state_snapshot))
    if missing:
        return ("NOT_AVAILABLE", missing)
    clone = copy.deepcopy(dict(state_snapshot))
    bounded = [e for e in (event_stream or [])
               if e.get("replay_seq", 0) <= (breach_replay_seq or 0)]
    clone["_breach_replay_seq"] = breach_replay_seq
    clone["_stream_prefix_hash"] = _canonical_hash(bounded)
    clone["_canonical_hash"] = _canonical_hash(clone)
    return clone


def clone_point_before_breach(event_seq, missing_fields=None,
                              actual_branch_mutated=None, event_stream=None,
                              state_snapshot=None):
    """Compatibility entry — delegates to clone_from_state.

    actual_branch_mutated is IGNORED by contract: the clone never sees
    state produced by the actual-release branch.
    """
    if missing_fields:
        return ("NOT_AVAILABLE", sorted(missing_fields))
    return clone_from_state(event_stream=event_stream,
                            breach_replay_seq=event_seq,
                            state_snapshot=state_snapshot)


def breach_snapshot(event, state_clone_hash, config_version):
    """Persist the breach snapshot (ts, loss-leg pnl, combined net, price,
    spread, z, ATR, clone hash, event seq, config version)."""
    return {
        "ts": event.get("ts"),
        "loss_leg_pnl": event.get("loss_leg_pnl", 0.0),
        "combined_net": event.get("combined_net", 0.0),
        "price": event.get("price", 0.0),
        "spread": event.get("spread", 0.0),
        "z": event.get("z", 0.0),
        "atr": event.get("atr", 0.0),
        "state_clone_hash": state_clone_hash,
        "event_seq": event.get("source_event_seq"),
        "config_version": config_version,
    }
