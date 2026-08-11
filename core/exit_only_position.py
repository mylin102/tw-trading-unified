"""Hydrate an attested RECONCILED_EXIT_ONLY broker pair into a managed
MTS position so the existing Policy J / combined / single-release exit
evaluation can produce ONLY capability-bound closing orders.

Fail-closed rules:
- hydrate only when a valid exit_only_capability exists (schema v2 with
  exact legs incl. broker avg costs); anything else raises a typed error
- the hydrated position carries broker-attested costs + trade_id and
  NEVER synthetic PnL
- every exit decision must bind reconciliation_id + position snapshot
  hash + a contemporaneous BBO snapshot hash/timestamp; missing / stale /
  ambiguous BBO -> N/A + zero order
- paper is never affected (callers gate on RECONCILED_EXIT_ONLY)
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Optional

from core.reconciled_exit import AttestationError

# Contemporaneous BBO window (ms) for an exit decision.
BBO_TTL_MS: int = 15_000

ENTRY_ACTIONS: frozenset = frozenset(
    {"BUY_NEAR_SELL_FAR", "SELL_NEAR_BUY_FAR"})


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def hydrate_exit_only_position(capability: Any) -> dict:
    """Build the managed MTS position state from an attested capability.

    Raises AttestationError with a typed code on any failure.
    """
    if not isinstance(capability, dict):
        raise AttestationError("EXIT_ONLY_CAPABILITY_MISSING",
                               "capability required")
    if capability.get("schema_version") != 2:
        raise AttestationError("EXIT_ONLY_CAPABILITY_INVALID",
                               "schema_version must be 2")
    trade_id = capability.get("trade_id")
    reconciliation_id = capability.get("reconciliation_id")
    snapshot_hash = capability.get("snapshot_hash")
    snapshot_captured_at = capability.get("snapshot_captured_at")
    if (not isinstance(trade_id, str) or not trade_id
            or not isinstance(reconciliation_id, str) or not reconciliation_id
            or not isinstance(snapshot_hash, str) or not snapshot_hash
            or not isinstance(snapshot_captured_at, int)):
        raise AttestationError("EXIT_ONLY_CAPABILITY_INVALID",
                               "identity fields missing")

    legs = capability.get("legs")
    if not isinstance(legs, (list, tuple)) or len(legs) != 2:
        raise AttestationError("EXIT_ONLY_CAPABILITY_INVALID",
                               "exactly two attested legs required")
    normalized = []
    seen = set()
    for leg in legs:
        if not isinstance(leg, dict):
            raise AttestationError("EXIT_ONLY_CAPABILITY_INVALID",
                                   "leg must be an object")
        code = leg.get("symbol")
        side = str(leg.get("side", "")).lower()
        qty = leg.get("remaining_qty")
        avg_cost = _positive_float(leg.get("avg_cost"))
        if (not isinstance(code, str) or not code
                or side not in ("buy", "sell")
                or not isinstance(qty, int) or isinstance(qty, bool)
                or qty <= 0 or avg_cost is None):
            raise AttestationError("EXIT_ONLY_CAPABILITY_INVALID",
                                   "attested leg invalid")
        key = (code, side)
        if key in seen:
            raise AttestationError("EXIT_ONLY_CAPABILITY_INVALID",
                                   "duplicate attested leg")
        seen.add(key)
        normalized.append({"code": code, "side": side,
                           "quantity": qty, "avg_cost": avg_cost})

    return {
        "trade_id": trade_id,
        "reconciliation_id": reconciliation_id,
        "snapshot_hash": snapshot_hash,
        "snapshot_captured_at": snapshot_captured_at,
        "has_position": True,
        "mode": "reconciled_exit_only",
        # open legs with broker-attested costs; no pnl fields (no synthetic PnL)
        "legs": normalized,
    }


def build_bbo_binding(bbo_slots: Any, *, now_ms: Optional[int] = None,
                      ttl_ms: int = BBO_TTL_MS) -> tuple:
    """Validate contemporaneous near/far BBO and return a decision binding.

    bbo_slots: {"near": {"bid", "ask", "bidask_at"}, "far": {...}}
    Returns (binding, None) or (None, reason) where reason is one of
    BBO_MISSING / BBO_STALE / BBO_AMBIGUOUS.
    """
    if not isinstance(bbo_slots, dict):
        return None, "BBO_MISSING"
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    slots = {}
    for leg in ("near", "far"):
        slot = bbo_slots.get(leg)
        if not isinstance(slot, dict):
            return None, "BBO_MISSING"
        bid = _positive_float(slot.get("bid"))
        ask = _positive_float(slot.get("ask"))
        ts = slot.get("bidask_at")
        if bid is None or ask is None:
            return None, "BBO_MISSING"
        if bid > ask:
            return None, "BBO_AMBIGUOUS"
        if not isinstance(ts, (int, float)) or ts <= 0:
            return None, "BBO_STALE"
        ts_ms = int(ts * 1000) if ts < 1e12 else int(ts)
        if now - ts_ms > ttl_ms:
            return None, "BBO_STALE"
        slots[leg] = {"bid": bid, "ask": ask, "bidask_at": ts_ms}

    payload = {
        "version": 1,
        "near": slots["near"],
        "far": slots["far"],
    }
    bbo_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    captured_at = max(slots["near"]["bidask_at"], slots["far"]["bidask_at"])
    return {"bbo_hash": bbo_hash, "bbo_captured_at": captured_at}, None


def attach_decision_binding(event: dict, capability: dict,
                            binding: dict) -> dict:
    """Return a copy of the decision event bound to capability + BBO."""
    merged = dict(event)
    merged["reconciliation_id"] = capability.get("reconciliation_id")
    merged["position_snapshot_hash"] = capability.get("snapshot_hash")
    merged["bbo_hash"] = binding.get("bbo_hash")
    merged["bbo_captured_at"] = binding.get("bbo_captured_at")
    return merged
