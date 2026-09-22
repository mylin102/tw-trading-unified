"""Pure broker-evidence order lifecycle decisions.

Phase 2 contract: broker evidence decides whether a local order remains
in-flight, reaches an explicit terminal state, or is a phantom.  This module
does not submit, cancel, mutate a broker, or write files.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import time
from typing import Any


TERMINAL_STATES = frozenset({
    "FILLED", "CANCELLED", "REJECTED", "EXPIRED", "RECEIVED_NOT_FILLED",
    "NOT_SENT",
})
PENDING_STATES = frozenset({
    "PENDING_UNCONFIRMED", "SUBMITTED", "PARTIAL_FILLED", "UNKNOWN",
})


@dataclass(frozen=True)
class LifecycleDecision:
    order_id: str
    state: str
    action: str
    reason: str
    broker_identity: str | None = None


def _value(row: Any, *names: str) -> Any:
    if isinstance(row, dict):
        for name in names:
            value = row.get(name)
            if value not in (None, ""):
                return value
        return None
    for name in names:
        value = getattr(row, name, None)
        if value not in (None, ""):
            return value
    return None


def _identities(row: Any) -> set[str]:
    """Only broker-issued fields participate; local order_id is excluded."""
    values = {
        _value(row, "broker_order_id", "id", "exchange_order_id"),
        _value(row, "ordno"),
        _value(row, "seqno"),
        _value(row, "deal_id", "trade_id", "fill_id"),
    }
    return {str(value) for value in values if value not in (None, "")}


def _preferred_identity(row: Any, intersection: set[str]) -> str | None:
    for name in ("broker_order_id", "id", "exchange_order_id", "ordno", "seqno"):
        value = _value(row, name)
        if value not in (None, "") and str(value) in intersection:
            return str(value)
    return next(iter(sorted(intersection)), None)


def _status(row: Any) -> str:
    raw = _value(row, "status")
    if isinstance(raw, dict):
        raw = raw.get("status")
    raw = getattr(raw, "value", raw)
    leaf = str(raw or "").split(".")[-1].strip().lower()
    return {
        "filled": "FILLED",
        "cancelled": "CANCELLED",
        "canceled": "CANCELLED",
        "rejected": "REJECTED",
        "failed": "REJECTED",
        "expired": "EXPIRED",
        "partfilled": "PARTIAL_FILLED",
        "partialfilled": "PARTIAL_FILLED",
        "partial_filled": "PARTIAL_FILLED",
        "submitted": "SUBMITTED",
        "presubmitted": "PENDING_UNCONFIRMED",
        "pre_submitted": "PENDING_UNCONFIRMED",
        "pendingsubmit": "PENDING_UNCONFIRMED",
        "pending_submit": "PENDING_UNCONFIRMED",
    }.get(leaf, "RECONCILE_REQUIRED")


def _valid_snapshot(snapshot: Any) -> bool:
    """Require fresh, complete broker truth; unrelated app mode is ignored."""
    if (not isinstance(snapshot, dict)
            or snapshot.get("source") != "live_broker"
            or snapshot.get("capture_error")
            or snapshot.get("query_failures")
            or snapshot.get("errors")
            or not isinstance(snapshot.get("positions"), list)
            or not isinstance(snapshot.get("open_orders"), list)):
        return False
    fetch_status = snapshot.get("fetch_status")
    if (fetch_status is not None
            and (not isinstance(fetch_status, dict)
                 or fetch_status.get("capture") != "OK")):
        return False
    captured_at = snapshot.get("captured_at")
    try:
        if isinstance(captured_at, str):
            parsed = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            captured_s = parsed.timestamp()
        else:
            captured_s = float(captured_at)
            if captured_s > 10_000_000_000:
                captured_s /= 1000.0
    except (TypeError, ValueError, OverflowError):
        return False
    age_s = time.time() - captured_s
    return math.isfinite(age_s) and -1.0 <= age_s <= 60.0


def _position_codes(local_order: Any) -> set[str]:
    codes = {str(_value(local_order, "symbol") or "")}
    legs = _value(local_order, "combo_legs")
    if isinstance(legs, list):
        codes.update(str(_value(leg, "symbol", "code") or "") for leg in legs)
    return {code for code in codes if code}


def _positions_unchanged(local_order: Any, snapshot: dict[str, Any]) -> bool:
    baseline = _value(local_order, "position_baseline")
    if not isinstance(baseline, list) or not isinstance(snapshot.get("positions"), list):
        return False

    def canonical(rows):
        return sorted((str(_value(row, "code")),
                       str(_value(row, "direction", "side")),
                       int(_value(row, "quantity", "qty", "net_quantity") or 0))
                      for row in rows)
    return canonical(baseline) == canonical(snapshot["positions"])


def _query_evidence(snapshot: dict[str, Any]) -> dict[str, Any]:
    value = snapshot.get("query_evidence")
    return value if isinstance(value, dict) else {}


def _deals_empty(deals: Any) -> bool:
    if not deals:
        return True
    if isinstance(deals, dict):
        return all(not rows for rows in deals.values())
    return not deals


def _decide_terminal(local_order, snapshot, evidence, status, identity):
    deal_quantity = evidence.get("deal_quantity")
    deals = evidence.get("deals")
    zero_deal = deal_quantity is not None and int(deal_quantity) == 0
    unchanged = _positions_unchanged(local_order, snapshot)
    complete = bool(evidence.get("window_complete"))
    if status in {"CANCELLED", "EXPIRED"} or status == "INACTIVE":
        if (bool(evidence.get("receipt") or _value(local_order, "broker_order_id"))
                and zero_deal and _deals_empty(deals) and unchanged and complete):
            return LifecycleDecision(
                str(_value(local_order, "order_id") or ""),
                "RECEIVED_NOT_FILLED", "APPLY_ZERO_FILL_TERMINAL",
                "EXPLICIT_TERMINAL_ZERO_DEAL_POSITIONS_UNCHANGED", identity)
        return LifecycleDecision(str(_value(local_order, "order_id") or ""),
                                 "PENDING_UNCONFIRMED", "RETAIN",
                                 "ZERO_FILL_EVIDENCE_INCOMPLETE", identity)
    if status == "FAILED":
        if (zero_deal and _deals_empty(deals) and unchanged and complete
                and bool(evidence.get("receipt") or identity)):
            return LifecycleDecision(str(_value(local_order, "order_id") or ""),
                                     "RECEIVED_NOT_FILLED", "APPLY_ZERO_FILL_TERMINAL",
                                     "FAILED_EXPLICIT_ZERO_DEAL", identity)
        return LifecycleDecision(str(_value(local_order, "order_id") or ""),
                                 "UNKNOWN", "RETAIN", "FAILED_EVIDENCE_INCOMPLETE", identity)
    return None


def reconcile_order(local_order: Any, snapshot: dict[str, Any], *,
                    reconciliation_window_complete: bool | None = None) -> LifecycleDecision:
    """Reconcile one order using explicit broker query/callback evidence.

    An empty projection is never terminal evidence.  ``NOT_SENT`` is reserved
    for the five-condition local exception/no-receipt/no-id/no-callback,
    completed-window, unchanged-baseline case.  All other ambiguity retains
    active protection and forbids resubmission.
    """
    order_id = str(_value(local_order, "order_id") or "")
    local_ids = _identities(local_order)
    identity = next(iter(sorted(local_ids)), None)
    local_status = str(getattr(_value(local_order, "status"), "value",
                               _value(local_order, "status")) or "").lower()
    if not _valid_snapshot(snapshot):
        return LifecycleDecision(order_id, "PENDING_UNCONFIRMED", "RETAIN",
                                 "BROKER_EVIDENCE_UNAVAILABLE", identity)
    evidence = _query_evidence(snapshot)
    if reconciliation_window_complete is not None:
        evidence = {**evidence, "window_complete": reconciliation_window_complete}
    if evidence.get("mismatch"):
        return LifecycleDecision(order_id, "UNKNOWN", "RETAIN",
                                 "BROKER_LOCAL_EVIDENCE_MISMATCH", identity)
    if not evidence:
        rows = (list(snapshot.get("broker_trades") or [])
                + list(snapshot.get("trades") or [])
                + list(snapshot.get("open_orders") or []))
        matched = next((row for row in rows if _identities(row) & local_ids), None)
        if matched is not None:
            evidence = {
                "query_available": True,
                "order_status": _status(matched),
                "broker_order_id": _preferred_identity(matched, _identities(matched) & local_ids),
                "receipt": True,
                "deal_quantity": _value(
                    matched, "deal_quantity", "filled_quantity", "filled_qty"),
                "deals": _value(matched, "deals") or [],
            }
        else:
            evidence = {"query_available": True}
    if not evidence.get("query_available", True):
        return LifecycleDecision(order_id, "PENDING_UNCONFIRMED", "RETAIN",
                                 "BROKER_QUERY_UNAVAILABLE", identity)

    status = str(evidence.get("order_status") or "").upper().replace(" ", "_")
    if status == "PARTFILLED":
        status = "PARTIAL_FILLED"
    if status == "INACTIVE":
        status = "INACTIVE"
    if status in {"FILLED", "PARTIAL_FILLED"}:
        return LifecycleDecision(order_id, status, "RETAIN" if status == "PARTIAL_FILLED" else "APPLY_TERMINAL",
                                 "EXPLICIT_BROKER_DEAL_STATUS", identity)
    _submitted_terminal_zero_fill = False
    if status in {"CANCELLED", "CANCELED", "EXPIRED", "INACTIVE", "FAILED"}:
        decision = _decide_terminal(local_order, snapshot, evidence,
                                    "CANCELLED" if status == "CANCELED" else status,
                                    identity)
        _deal_qty = evidence.get("deal_quantity")
        _submitted_terminal_zero_fill = (
            local_status == "submitted"
            and status in {"CANCELLED", "CANCELED", "EXPIRED", "INACTIVE"}
            and (_deal_qty is None or int(_deal_qty) == 0)
            and _deals_empty(evidence.get("deals")))
        if decision is not None and not _submitted_terminal_zero_fill:
            return decision

    has_callback = bool(evidence.get("callbacks"))
    local_receipt = _value(local_order, "receipt")
    local_broker_id = _value(local_order, "broker_order_id")
    has_receipt = bool(local_receipt if local_receipt is not None else evidence.get("receipt"))
    has_broker_id = bool(local_broker_id if local_broker_id is not None else evidence.get("broker_order_id"))
    no_callback = not has_callback
    no_broker_id = not has_broker_id
    no_receipt = not has_receipt
    no_order_or_deal_callback = not bool(evidence.get("order_callback") or evidence.get("deal_callback"))
    if (bool(_value(local_order, "local_submit_exception")) and no_receipt
            and no_broker_id and no_callback and no_order_or_deal_callback
            and not evidence.get("order_query_result")
            and bool(evidence.get("window_complete"))
            and _positions_unchanged(local_order, snapshot)):
        return LifecycleDecision(order_id, "NOT_SENT", "RETAIN_AUDIT",
                                 "LOCAL_EXCEPTION_NO_RECEIPT_NO_BROKER_EVIDENCE", None)
    if ((status and not _submitted_terminal_zero_fill)
            or evidence.get("order_query_result") is False):
        return LifecycleDecision(order_id, "PENDING_UNCONFIRMED", "RETAIN",
                                 "RECONCILIATION_INCOMPLETE", identity)
    position_codes = _position_codes(local_order)
    if any(str(_value(pos, "code")) in position_codes
           for pos in snapshot.get("positions", [])):
        return LifecycleDecision(order_id, "PENDING_UNCONFIRMED", "RETAIN",
                                 "BROKER_POSITION_PRESENT", identity)
    if local_status == "submitted":
        return LifecycleDecision(
            order_id, "RECEIVED_NOT_FILLED", "APPLY_ZERO_FILL_TERMINAL",
            "FRESH_BROKER_ORDER_DEAL_POSITION_ABSENT", identity)
    return LifecycleDecision(order_id, "PENDING_UNCONFIRMED", "RETAIN",
                             "BROKER_ORDER_NOT_FOUND_REQUIRES_WINDOW", identity)
