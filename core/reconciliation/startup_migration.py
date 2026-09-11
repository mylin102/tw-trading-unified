"""Sanctioned startup migration of a stale live context."""

from __future__ import annotations

from core.reconciliation.holding_state import reconcile_holding


def _legs_from_snapshot(snapshot: dict) -> list[dict]:
    result = []
    for row in snapshot.get("positions", []):
        if not isinstance(row, dict) or row.get("account") != "futures":
            continue
        text = str(row.get("direction", "")).lower()
        result.append({
            "code": row["code"],
            "side": "SHORT" if "sell" in text or "short" in text else "LONG" if "buy" in text or "long" in text else "",
            "qty": row["quantity"], "avg_cost": row["avg_cost"],
        })
    return result


def _legs_from_stale_context(ctx: dict) -> list[dict] | None:
    cap = ctx.get("exit_only_capability")
    rows = cap.get("legs") if isinstance(cap, dict) else None
    if not isinstance(rows, list):
        return None
    return [{
        "code": row["symbol"],
        "side": "SHORT" if str(row.get("side", "")).lower() == "sell" else "LONG",
        "qty": row["remaining_qty"], "avg_cost": row["avg_cost"],
    } for row in rows if isinstance(row, dict)]


def migrate_stale_live_context(runtime_dir: str, snapshot: dict,
                               *, tick_size: float = 1.0) -> tuple[bool, dict]:
    """Reconcile and atomically replace stale ctx; never calls broker APIs."""
    from core.execution_context_state import persist_execution_context, read_execution_context
    ctx = read_execution_context(runtime_dir=runtime_dir)
    capture_ok = ((snapshot or {}).get("fetch_status") or {}).get("capture") == "OK"
    decision = reconcile_holding(
        _legs_from_snapshot(snapshot or {}) if capture_ok else None,
        _legs_from_stale_context(ctx), tick_size=tick_size,
        broker_query_error=None if capture_ok else "BROKER_CAPTURE_UNCERTAIN")
    evidence = {"status": decision.status.value, "reason": decision.reason,
                "raw_broker_legs": list(decision.raw_broker_legs),
                "raw_local_legs": list(decision.raw_local_legs),
                "canonical_legs": list(decision.canonical_legs)}
    payload = dict(ctx)
    payload.update({
        "requested_mode": "live",
        "effective_mode": "live_ready" if not decision.fail_closed else "live_quarantined",
        "live_order_allowed": not decision.fail_closed,
        "audit_reasons": [] if not decision.fail_closed else ["RECONCILIATION_HOLD", decision.reason],
        "exit_only_capability": None,
        "canonical_holding": evidence,
    })
    persisted = persist_execution_context(payload, runtime_dir=runtime_dir)
    return not decision.fail_closed, persisted
