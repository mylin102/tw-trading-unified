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


def _legs_from_fill_ledger(runtime_dir: str) -> tuple[list[dict] | None, str | None]:
    """Read durable BROKER_FILL rows as primary local position evidence."""
    ledger = Path(runtime_dir) / "logs" / "mts_trade_fills.jsonl"
    latest = {}
    try:
        for line in ledger.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if (row.get("trade_id", "").startswith("broker-reconciled-")
                    and row.get("execution_source") == "BROKER_FILL"):
                latest[row.get("instrument_code")] = row
    except (OSError, ValueError, TypeError):
        return None, None
    if not latest:
        return None, None
    trade_ids = {str(row.get("trade_id")) for row in latest.values()}
    if len(trade_ids) != 1:
        return None, None
    return [{
        "code": row.get("instrument_code"),
        "side": "LONG" if str(row.get("side", "")).upper() == "BUY" else "SHORT",
        "qty": row.get("quantity") or row.get("qty"),
        "avg_cost": row.get("price") or row.get("avg_price"),
    } for row in latest.values()], next(iter(trade_ids))


def _legs_from_stale_context(ctx: dict, runtime_dir: str) -> tuple[list[dict] | None, str | None, str]:
    # The durable fill ledger is primary after restart.
    legs, trade_id = _legs_from_fill_ledger(runtime_dir)
    if legs is not None:
        return legs, trade_id, "durable_broker_fill_ledger"
    cap = ctx.get("exit_only_capability")
    rows = cap.get("legs") if isinstance(cap, dict) else None
    if isinstance(rows, list):
        return ([{
            "code": row["symbol"],
            "side": "SHORT" if str(row.get("side", "")).lower() == "sell" else "LONG",
            "qty": row["remaining_qty"], "avg_cost": row["avg_cost"],
        } for row in rows if isinstance(row, dict)], cap.get("trade_id"), "legacy_context")
    return None, None, "none"


def _state_proves_settled_flat(runtime_dir: str) -> bool:
    """Return true only for the explicit persisted flat/settled state.

    Historical fill rows are not open-position evidence after a fresh broker
    flat capture.  This exception is deliberately narrow: a missing,
    unreadable, non-flat, or non-settled state never clears reconciliation.
    """
    path = Path(runtime_dir) / "data" / "state" / "mts_position_state.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        return (state.get("has_position") is False
                and str(state.get("state", "")).upper() == "FLAT"
                and str(state.get("settlement_status", "")).upper() == "SETTLED")
    except (OSError, ValueError, TypeError):
        return False


def migrate_stale_live_context(runtime_dir: str, snapshot: dict,
                               *, tick_size: float = 1.0) -> tuple[bool, dict]:
    """Reconcile and atomically replace stale ctx; never calls broker APIs."""
    from core.execution_context_state import persist_execution_context, read_execution_context
    ctx = read_execution_context(runtime_dir=runtime_dir)
    capture_ok = ((snapshot or {}).get("fetch_status") or {}).get("capture") == "OK"
    local_legs, ledger_trade_id, evidence_source = _legs_from_stale_context(ctx, runtime_dir)
    broker_legs = _legs_from_snapshot(snapshot or {}) if capture_ok else None
    if (capture_ok and broker_legs == []
            and _state_proves_settled_flat(runtime_dir)):
        # Current reconciliation is authoritative: a fresh broker-flat
        # snapshot plus explicit settled-flat state means the ledger's entry
        # rows are historical, not current holding legs.  Evaluate this on
        # every migration call so a stale HOLD cannot persist across rewrites.
        local_legs = []
        evidence_source = "broker_flat_settled_reconcile"
    canonical = ctx.get("canonical_holding")
    if local_legs is not None and isinstance(canonical, dict) and canonical.get("canonical_legs"):
        secondary = reconcile_holding(local_legs, canonical.get("canonical_legs"), tick_size=tick_size)
        if secondary.fail_closed:
            local_legs = None
            evidence_source = "ledger_canonical_mismatch"
    decision = reconcile_holding(
        broker_legs,
        local_legs, tick_size=tick_size,
        broker_query_error=None if capture_ok else "BROKER_CAPTURE_UNCERTAIN")
    evidence = {"status": decision.status.value, "reason": decision.reason,
                "trade_id": ledger_trade_id or "",
                "evidence_hash": (snapshot or {}).get("canonical_input_hash") or
                    ((canonical or {}).get("evidence_hash", "") if isinstance(canonical, dict) else ""),
                "source": evidence_source,
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
