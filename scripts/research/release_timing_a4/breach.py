"""Threshold breach snapshot + pre-breach clone — A4 engine.

The clone is REAL delegation to the canonical
phase_transition_replay.clone.clone_from_state — the deep-copy/hash/prefix
primitives live in exactly ONE place (the canonical module). The A4 module
only adapts the A4 schema field set; the canonical strictly-before-breach
semantics (replay_seq < breach_replay_seq) apply unchanged.
"""

from scripts.research.phase_transition_replay import clone as _replay_clone  # noqa: F401
from scripts.research.phase_transition_replay.clone import (
    clone_schema_version as _canonical_schema_version,  # noqa: F401
)

CLONE_SCHEMA_FIELDS = {
    "positions", "policy_peak", "guard_warmup", "guard_armed",
    "atr", "reference_prices", "pending_candidates", "pending_orders",
    "quote_freshness", "controller", "lifecycle", "cooldown",
    "strategy_generation", "config_version",
}

SCHEMA_VERSION = "a4-v1"


def clone_schema_version():
    """A4 schema version + field set (field-name keys)."""
    return {"version": SCHEMA_VERSION,
            **{f: "state" for f in sorted(CLONE_SCHEMA_FIELDS)}}


def clone_from_state(event_stream, breach_replay_seq, state_snapshot):
    """A4 schema adapter — delegates to the CANONICAL clone primitive.

    All deep-copy/hash/prefix logic lives in
    phase_transition_replay.clone.clone_from_state (single source).
    """
    return _replay_clone.clone_from_state(
        event_stream=event_stream, breach_replay_seq=breach_replay_seq,
        state_snapshot=state_snapshot, schema_fields=CLONE_SCHEMA_FIELDS)


def clone_point_before_breach(event_seq, missing_fields=None,
                              actual_branch_mutated=None, event_stream=None,
                              state_snapshot=None):
    """Compatibility entry — delegates to the canonical clone.

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
