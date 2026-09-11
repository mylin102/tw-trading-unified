"""Sanctioned startup migration of a stale live context."""

from __future__ import annotations

import json
from pathlib import Path

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


def _legs_from_stale_context(ctx: dict, runtime_dir: str) -> list[dict] | None:
    cap = ctx.get("exit_only_capability")
    rows = cap.get("legs") if isinstance(cap, dict) else None
    if isinstance(rows, list):
        return [{
        "code": row["symbol"],
        "side": "SHORT" if str(row.get("side", "")).lower() == "sell" else "LONG",
        "qty": row["remaining_qty"], "avg_cost": row["avg_cost"],
        } for row in rows if isinstance(row, dict)]
    # After a restart the old ctx may already have been rewritten without
    # the legacy capability.  Use durable BROKER_FILL ledger rows as the
    # local side of reconciliation; never infer from broker shape alone.
    ledger = Path(runtime_dir) / "logs" / "mts_trade_fills.jsonl"
    latest = {}
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if (row.get("trade_id", "").startswith("broker-reconciled-")
                    and row.get("execution_source") == "BROKER_FILL"):
                latest[row.get("instrument_code")] = row
    except (OSError, ValueError, TypeError):
        return None
    return [{
        "code": row.get("instrument_code"),
        "side": "LONG" if str(row.get("side", "")).upper() == "BUY" else "SHORT",
        "qty": row.get("quantity") or row.get("qty"),
        "avg_cost": row.get("price") or row.get("avg_price"),
    } for row in latest.values()]


def migrate_stale_live_context(runtime_dir: str, snapshot: dict,
                               *, tick_size: float = 1.0) -> tuple[bool, dict]:
    """Reconcile and atomically replace stale ctx; never calls broker APIs."""
    from core.execution_context_state import persist_execution_context, read_execution_context
    ctx = read_execution_context(runtime_dir=runtime_dir)
    capture_ok = ((snapshot or {}).get("fetch_status") or {}).get("capture") == "OK"
    decision = reconcile_holding(
        _legs_from_snapshot(snapshot or {}) if capture_ok else None,
        _legs_from_stale_context(ctx, runtime_dir), tick_size=tick_size,
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
