"""Fail-closed broker premise guards for MTS release/exit decisions.

The guard is deliberately pure: it does not submit, cancel, or mutate a
broker order.  Callers must invoke it immediately before creating a release
order and must keep the returned audit record.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class PremiseError(ValueError):
    """An order intent omitted or malformed its required premise."""


@dataclass(frozen=True)
class ReleaseIntent:
    action: str
    symbol: str
    side: str
    qty: int
    premise: dict[str, Any] | None

    def __post_init__(self) -> None:
        if not isinstance(self.premise, dict):
            raise PremiseError("premise is required")
        kind = str(self.premise.get("kind", "")).upper()
        if kind not in {"HOLD_LEG", "NOT_HOLD", "HOLD_ANY"}:
            raise PremiseError(f"unsupported premise kind: {kind or '<missing>'}")
        if kind == "HOLD_LEG":
            if not self.premise.get("leg") or not self.premise.get("side"):
                raise PremiseError("HOLD_LEG requires leg and side")
            try:
                if int(self.premise.get("qty", 0)) < 1:
                    raise PremiseError("HOLD_LEG qty must be >= 1")
            except (TypeError, ValueError) as exc:
                raise PremiseError("HOLD_LEG qty must be an integer") from exc


@dataclass(frozen=True)
class GuardDecision:
    allowed: bool
    reason: str
    audit: dict[str, Any]


@dataclass(frozen=True)
class StartupDecision:
    release_allowed: bool
    reason: str
    state: dict[str, Any]
    audit: dict[str, Any]


def _positions(snapshot: Any) -> list[dict[str, Any]] | None:
    if not isinstance(snapshot, dict):
        return None
    rows = snapshot.get("positions")
    return rows if isinstance(rows, list) else None


def _snapshot_available(snapshot: Any) -> bool:
    return (
        isinstance(snapshot, dict)
        and snapshot.get("source") == "live_broker"
        and (_fetch_status(snapshot).get("capture") == "OK")
        and _positions(snapshot) is not None
    )


def _fetch_status(snapshot: dict[str, Any]) -> dict[str, Any]:
    status = snapshot.get("fetch_status")
    return status if isinstance(status, dict) else {}


def _audit(*, event: str, reason: str, intent: ReleaseIntent | None,
           snapshot: Any, stop_further_orders: bool = False) -> dict[str, Any]:
    return {
        "event": event,
        "reason": reason,
        "action": intent.action if intent else "STARTUP",
        "symbol": intent.symbol if intent else None,
        "premise": intent.premise if intent else None,
        "broker_truth": repr((_positions(snapshot) if isinstance(snapshot, dict) else None)),
        "stop_further_orders": stop_further_orders,
    }


def _matching_position(positions: list[dict[str, Any]], symbol: str) -> dict[str, Any] | None:
    for row in positions:
        if str(row.get("code") or row.get("symbol") or "") == str(symbol):
            return row
    return None


def _canonical_side(value: Any) -> str:
    """Normalize a broker position direction to canonical LONG/SHORT.

    Shioaji 1.5.5 FuturePosition.direction stringifies to 'Action.Buy' /
    'Action.Sell' (a qualified enum string, not the bare name), so the
    snapshot direction never equals the premise's LONG/SHORT after a naive
    upper().  Map 'Action.Buy'/'Buy'/'BUY'/'LONG' -> LONG and
    'Action.Sell'/'Sell'/'SELL'/'SHORT' -> SHORT; unknown values are returned
    upper-cased so the caller still fails closed.
    """
    v = str(value or "").upper()
    if v in ("BUY", "ACTION.BUY"):
        return "LONG"
    if v in ("SELL", "ACTION.SELL"):
        return "SHORT"
    return v


def evaluate_release_precondition(intent: ReleaseIntent,
                                  broker_snapshot: dict[str, Any]) -> GuardDecision:
    """Require live broker truth to satisfy the intent's premise."""
    if not _snapshot_available(broker_snapshot):
        reason = "BROKER_QUERY_UNAVAILABLE"
        return GuardDecision(False, reason,
                             _audit(event="ORDER_INTENT_BLOCKED", reason=reason,
                                    intent=intent, snapshot=broker_snapshot))

    positions = _positions(broker_snapshot) or []
    premise = intent.premise or {}
    kind = str(premise.get("kind", "")).upper()
    target = _matching_position(positions, intent.symbol)
    if kind == "NOT_HOLD":
        ok = target is None
    elif kind == "HOLD_ANY":
        ok = bool(positions)
    else:
        expected_side = str(premise.get("side", "")).upper()
        try:
            expected_qty = int(premise.get("qty", 0))
            actual_qty = abs(int((target or {}).get("qty") or (target or {}).get("quantity") or 0))
        except (TypeError, ValueError):
            expected_qty, actual_qty = 0, 0
        actual_side = _canonical_side(
            (target or {}).get("side") or (target or {}).get("direction"))
        ok = bool(target and actual_side == expected_side and actual_qty >= expected_qty)

    if not ok:
        reason = "BROKER_PREMISE_MISMATCH"
        return GuardDecision(False, reason,
                             _audit(event="ORDER_INTENT_BLOCKED", reason=reason,
                                    intent=intent, snapshot=broker_snapshot))
    return GuardDecision(True, "BROKER_PREMISE_CONFIRMED",
                         _audit(event="ORDER_PREMISE_CONFIRMED",
                                reason="BROKER_PREMISE_CONFIRMED",
                                intent=intent, snapshot=broker_snapshot))


def _qty(snapshot: dict[str, Any], symbol: str) -> int | None:
    rows = _positions(snapshot)
    if rows is None:
        return None
    row = _matching_position(rows, symbol)
    if row is None:
        return 0
    try:
        return abs(int(row.get("qty") or row.get("quantity") or 0))
    except (TypeError, ValueError):
        return None


def validate_release_effect(*, before: dict[str, Any], after: dict[str, Any],
                            symbol: str) -> GuardDecision:
    """A release may not increase the broker position of its target leg."""
    if not _snapshot_available(before) or not _snapshot_available(after):
        reason = "POST_RELEASE_QUERY_UNAVAILABLE"
        return GuardDecision(False, reason, {
            "event": "POST_RELEASE_EFFECT_BLOCKED",
            "reason": reason,
            "symbol": symbol,
            "stop_further_orders": True,
        })
    before_qty, after_qty = _qty(before, symbol), _qty(after, symbol)
    if before_qty is None or after_qty is None:
        reason = "POST_RELEASE_QTY_UNREADABLE"
        return GuardDecision(False, reason, {
            "event": "POST_RELEASE_EFFECT_BLOCKED",
            "reason": reason,
            "symbol": symbol,
            "stop_further_orders": True,
        })
    if after_qty > before_qty:
        reason = "POST_RELEASE_POSITION_INCREASE"
        return GuardDecision(False, reason, {
            "event": "POST_RELEASE_EFFECT_BLOCKED",
            "reason": reason,
            "symbol": symbol,
            "before_qty": before_qty,
            "after_qty": after_qty,
            "stop_further_orders": True,
        })
    return GuardDecision(True, "POST_RELEASE_EFFECT_OK", {
        "event": "POST_RELEASE_EFFECT_CONFIRMED",
        "reason": "POST_RELEASE_EFFECT_OK",
        "symbol": symbol,
        "before_qty": before_qty,
        "after_qty": after_qty,
        "stop_further_orders": False,
    })


def sanitize_startup_state(state: dict[str, Any], broker_snapshot: dict[str, Any],
                           *, near_symbol: str, far_symbol: str) -> StartupDecision:
    """Reconcile decision inputs from broker truth before lifecycle evaluation."""
    clean = dict(state)
    if not _snapshot_available(broker_snapshot):
        reason = "BROKER_QUERY_UNAVAILABLE"
        return StartupDecision(False, reason, clean,
                               _audit(event="STARTUP_ORDER_BLOCKED", reason=reason,
                                      intent=None, snapshot=broker_snapshot,
                                      stop_further_orders=True))
    positions = _positions(broker_snapshot) or []
    near = _matching_position(positions, near_symbol)
    far = _matching_position(positions, far_symbol)
    broker_legs = []
    for leg_name, row in (("near", near), ("far", far)):
        if row:
            broker_legs.append({
                "leg": leg_name,
                "status": "OPEN",
                "side": str(row.get("side") or row.get("direction") or "").upper() or None,
                "qty": abs(int(row.get("qty") or row.get("quantity") or 0)),
                "entry": row.get("avg_cost"),
            })
    local_legs = state.get("legs")
    legs_diverged = isinstance(local_legs, list) and local_legs != broker_legs
    if not near and not far:
        for field in ("near_side", "far_side", "released_leg", "remaining_leg",
                      "near_entry", "far_entry"):
            clean[field] = None
        clean["has_position"] = False
        clean["state"] = "FLAT"
        clean["legs"] = []
        reason = "BROKER_FLAT_NO_RELEASE"
        return StartupDecision(False, reason, clean,
                               _audit(event="STARTUP_ORDER_BLOCKED", reason=reason,
                                      intent=None, snapshot=broker_snapshot,
                                      stop_further_orders=True))
    if near:
        clean["near_side"] = str(near.get("side") or near.get("direction") or "").upper() or None
        clean["near_entry"] = near.get("avg_cost")
    if far:
        clean["far_side"] = str(far.get("side") or far.get("direction") or "").upper() or None
        clean["far_entry"] = far.get("avg_cost")
    clean["has_position"] = True
    clean["state"] = "POSITION"
    clean["legs"] = broker_legs
    audit = _audit(event="STARTUP_RECONCILED",
                   reason="BROKER_POSITION_RECONCILED",
                   intent=None, snapshot=broker_snapshot)
    if legs_diverged:
        audit.update({"local_legs": local_legs, "broker_legs": broker_legs,
                      "legs_action": "RECONCILED_FROM_BROKER"})
    return StartupDecision(True, "BROKER_POSITION_RECONCILED", clean,
                           audit)
