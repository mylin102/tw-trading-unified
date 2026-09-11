"""Canonical holding reconciliation for startup and reconnect.

The broker snapshot is retained verbatim as evidence.  A matching snapshot
hydrates the ordinary strategy position; it never creates a second exit
authorization mode.  Any uncertainty remains fail-closed in a
``reconciliation_hold``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Sequence


class ReconciliationStatus(str, Enum):
    RECONCILED = "RECONCILED"
    HOLD = "HOLD"


@dataclass(frozen=True)
class HoldingReconciliation:
    status: ReconciliationStatus
    context_mode: str
    hydrated: bool
    entry_blocked: bool
    fail_closed: bool
    reason: str = ""
    raw_broker_legs: tuple[dict, ...] = ()
    raw_local_legs: tuple[dict, ...] = ()
    canonical_legs: tuple[dict, ...] = ()
    exit_only_capability: None = None

    @property
    def quarantine(self) -> bool:
        """Compatibility spelling for callers of the removed old mode."""
        return self.status is ReconciliationStatus.HOLD


def _raw(legs: Sequence[dict] | None) -> tuple[dict, ...]:
    return tuple(dict(leg) for leg in legs or () if isinstance(leg, dict))


def reconcile_holding(
    broker_legs: Sequence[dict] | None,
    local_legs: Sequence[dict] | None,
    *,
    tick_size: float,
    broker_query_error: str | None = None,
) -> HoldingReconciliation:
    """Compare broker and local canonical legs with an explicit tick rule.

    Average cost is equal when the absolute difference is at most half a
    tick.  This is deliberately stricter than the legacy broad ``0.5`` rule
    and applies only after code, side and quantity match.
    """
    broker_raw = _raw(broker_legs)
    local_raw = _raw(local_legs)
    if broker_query_error or broker_legs is None:
        return HoldingReconciliation(
            ReconciliationStatus.HOLD, "reconciliation_hold", False, True,
            True, broker_query_error or "BROKER_POSITIONS_UNAVAILABLE",
            broker_raw, local_raw)
    if not isinstance(tick_size, (int, float)) or tick_size <= 0:
        return HoldingReconciliation(
            ReconciliationStatus.HOLD, "reconciliation_hold", False, True,
            True, "INVALID_TICK_SIZE", broker_raw, local_raw)
    if len(broker_raw) != len(local_raw):
        reason = "LEG_COUNT_MISMATCH"
    else:
        reason = ""
        for broker, local in zip(broker_raw, local_raw):
            if (broker.get("code") != local.get("code")
                    or str(broker.get("side", "")).upper()
                    != str(local.get("side", "")).upper()
                    or broker.get("qty") != local.get("qty")):
                reason = "LEG_IDENTITY_MISMATCH"
                break
            try:
                if abs(float(broker["avg_cost"])
                       - float(local["avg_cost"])) > float(tick_size) / 2:
                    reason = "AVG_COST_MISMATCH"
                    break
            except (KeyError, TypeError, ValueError):
                reason = "AVG_COST_UNREADABLE"
                break
    if reason:
        return HoldingReconciliation(
            ReconciliationStatus.HOLD, "reconciliation_hold", False, True,
            True, reason, broker_raw, local_raw)
    return HoldingReconciliation(
        ReconciliationStatus.RECONCILED, "live_ready", True,
        bool(broker_raw), False, "", broker_raw, local_raw, broker_raw, None)
