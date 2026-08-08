"""Threshold breach snapshot + pre-breach clone — A4 engine.

Clone contract: taken immediately BEFORE the breach event (never the
actual-release future state); a value-complete clone covers the full schema;
ANY missing field yields a typed ("NOT_AVAILABLE", [missing fields]) result.
"""

CLONE_SCHEMA_FIELDS = {
    "positions", "policy_peak", "guard_warmup", "guard_armed",
    "atr", "reference_prices", "pending_candidates", "pending_orders",
    "quote_freshness", "controller", "lifecycle", "cooldown",
    "strategy_generation", "config_version",
}

SCHEMA_VERSION = "a4-v1"


def clone_schema_version():
    """Schema version + the full field set (each field keyed with a type
    descriptor)."""
    return {"version": SCHEMA_VERSION,
            **{f: "state" for f in sorted(CLONE_SCHEMA_FIELDS)}}


def clone_point_before_breach(event_seq, missing_fields=None,
                              actual_branch_mutated=None):
    """Value-complete clone at the event BEFORE the breach.

    - missing_fields: any non-empty set -> typed NOT_AVAILABLE naming the
      exact missing fields (never a silent partial clone).
    - actual_branch_mutated: IGNORED by contract — the clone never sees
      state produced by the actual-release branch.
    """
    missing = set(missing_fields or {})
    if missing:
        return ("NOT_AVAILABLE", sorted(missing))
    clone = {f: f"<{f}>" for f in sorted(CLONE_SCHEMA_FIELDS)}
    clone["event_seq"] = event_seq
    clone["schema_version"] = SCHEMA_VERSION
    return clone


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
