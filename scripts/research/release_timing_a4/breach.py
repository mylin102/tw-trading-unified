"""Threshold breach snapshot — skeletal (A4 v2: clone schema expanded)."""

CLONE_SCHEMA_FIELDS = {
    "positions", "policy_peak", "guard_warmup", "guard_armed",
    "atr", "reference_prices", "pending_candidates", "pending_orders",
    "quote_freshness", "controller", "lifecycle", "cooldown",
    "strategy_generation", "config_version",
}


def clone_schema_version():
    raise NotImplementedError("breach.clone_schema_version: schema version; non-reconstructible state -> NOT_AVAILABLE")


def clone_point_before_breach(event_seq):
    raise NotImplementedError("breach.clone_point_before_breach: clone taken at the event BEFORE the breach, never actual-release future state")


def breach_snapshot(event, state_clone_hash, config_version):
    raise NotImplementedError("breach.breach_snapshot: ts/loss-leg pnl/combined net/price/spread/z/ATR/clone hash/seq/config version")
