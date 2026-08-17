"""Pure broker-evidence order lifecycle decisions.

Phase 2 contract: broker evidence decides whether a local order remains
in-flight, reaches an explicit terminal state, or is a phantom.  This module
does not submit, cancel, mutate a broker, or write files.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


TERMINAL_STATES = frozenset({"FILLED", "CANCELLED", "REJECTED", "EXPIRED"})
PENDING_STATES = frozenset({"PENDING_UNCONFIRMED", "SUBMITTED", "PARTIAL_FILLED"})


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
    return (
        isinstance(snapshot, dict)
        and snapshot.get("source") == "live_broker"
        and not snapshot.get("capture_error")
        and isinstance(snapshot.get("positions"), list)
        and isinstance(snapshot.get("open_orders"), list)
    )


def reconcile_order(local_order: Any, snapshot: dict[str, Any]) -> LifecycleDecision:
    """Return a deterministic, side-effect-free broker lifecycle decision.

    Query failure/identity ambiguity retain the order and require reconcile;
    no branch requests a submit or cancel operation.
    """
    order_id = str(_value(local_order, "order_id") or "")
    local_ids = _identities(local_order)
    if not _valid_snapshot(snapshot):
        return LifecycleDecision(order_id, "RECONCILE_REQUIRED", "RETAIN",
                                 "BROKER_EVIDENCE_UNAVAILABLE")
    if not local_ids:
        return LifecycleDecision(order_id, "RECONCILE_REQUIRED", "RETAIN",
                                 "BROKER_IDENTITY_MISSING")

    all_rows = list(snapshot.get("trades") or [])
    terminal_rows = [row for row in all_rows if _identities(row) & local_ids]
    for row in terminal_rows:
        state = _status(row)
        if state in TERMINAL_STATES:
            return LifecycleDecision(order_id, state, "APPLY_TERMINAL",
                                     "EXPLICIT_BROKER_TERMINAL",
                                     _preferred_identity(row, _identities(row) & local_ids))

    for row in snapshot["open_orders"]:
        matched = _identities(row) & local_ids
        if matched:
            state = _status(row)
            if state in TERMINAL_STATES:
                return LifecycleDecision(order_id, state, "APPLY_TERMINAL",
                                         "EXPLICIT_BROKER_TERMINAL",
                                         _preferred_identity(row, matched))
            if state == "PARTIAL_FILLED":
                return LifecycleDecision(order_id, state, "RETAIN",
                                         "BROKER_PARTIAL_FILLED",
                                         _preferred_identity(row, matched))
            return LifecycleDecision(order_id, "PENDING_UNCONFIRMED", "RETAIN",
                                     "BROKER_ORDER_PRESENT",
                                     _preferred_identity(row, matched))

    symbol = _value(local_order, "symbol")
    if symbol and any(str(_value(pos, "code")) == str(symbol)
                      for pos in snapshot["positions"]):
        return LifecycleDecision(order_id, "PENDING_UNCONFIRMED", "RETAIN",
                                 "BROKER_POSITION_PRESENT")

    return LifecycleDecision(order_id, "BROKER_NOT_FOUND", "MARK_TERMINAL",
                             "BROKER_ORDER_AND_POSITION_ABSENT")
