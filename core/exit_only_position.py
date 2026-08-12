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
BBO_SKEW_MS: int = 1_000  # [S2] named conservative cross-leg skew bound

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
                      ttl_ms: int = BBO_TTL_MS,
                      skew_ms: int = BBO_SKEW_MS,
                      near_code: Optional[str] = None,
                      far_code: Optional[str] = None,
                      identity: Optional[dict] = None) -> tuple:
    """[S2] Validate contemporaneous near/far BBO + decision identity and
    return a version-2 canonical binding (hash + raw payload).

    Each leg (dedicated EXIT_ONLY BBO evidence cache entry) requires:
    exact expected contract code (BBO_CODE_MISMATCH), source ==
    "shioaji_bidask" (BBO_SOURCE_MISMATCH), positive finite bid <= ask
    (BBO_MISSING / BBO_AMBIGUOUS), finite int epoch-ms EXCHANGE timestamp
    (BBO_STALE), not future beyond 1s (BBO_FUTURE), TTL <= 15s
    (BBO_STALE); cross-leg skew <= 1s on EXCHANGE timestamps (BBO_SKEW).
    The decision identity (cap reconciliation_id, position snapshot hash,
    config_hash, release_sha, session_id) must be present
    (BBO_IDENTITY_MISSING).  The version-2 hash AND the raw payload bind
    raw bid/ask, exchange timestamps, received_at, source, symbols and
    the identity — any bad dimension returns (None, reason) =>
    N/A/BLOCKED, zero evaluator submit/order.
    """
    if not isinstance(bbo_slots, dict):
        return None, "BBO_MISSING"
    if not isinstance(identity, dict):
        return None, "BBO_IDENTITY_MISSING"
    _id_keys = ("reconciliation_id", "snapshot_hash", "config_hash",
                "release_sha", "session_id")
    if any(not identity.get(k) for k in _id_keys):
        return None, "BBO_IDENTITY_MISSING"
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    expected = {"near": near_code, "far": far_code}
    slots = {}
    for leg in ("near", "far"):
        slot = bbo_slots.get(leg)
        if not isinstance(slot, dict):
            return None, "BBO_MISSING"
        bid = _positive_float(slot.get("bid"))
        ask = _positive_float(slot.get("ask"))
        if bid is None or ask is None:
            return None, "BBO_MISSING"
        if slot.get("code") != expected[leg]:
            return None, "BBO_CODE_MISMATCH"
        if slot.get("source") != "shioaji_bidask":
            return None, "BBO_SOURCE_MISMATCH"
        if bid > ask:
            return None, "BBO_AMBIGUOUS"
        ts = slot.get("exchange_ts_ms")
        # [S2 audit] strict canonical timestamp: type(x) is int, not
        # bool, >= 1e12 (epoch-ms).  float/seconds/missing are rejected —
        # the binding NEVER rescales seconds-or-float timestamps.
        if (not isinstance(ts, int) or isinstance(ts, bool)
                or ts < 1e12):
            return None, "BBO_STALE"
        ts_ms = ts
        if ts_ms > now + 1_000:
            return None, "BBO_FUTURE"
        if now - ts_ms > ttl_ms:
            return None, "BBO_STALE"
        slots[leg] = {
            "symbol": slot.get("code"), "bid": bid, "ask": ask,
            "exchange_ts": ts_ms,
            "received_at_ms": slot.get("received_at_ms"),
            "source": slot.get("source"),
            "seq": slot.get("seq"),
        }

    if abs(slots["near"]["exchange_ts"] - slots["far"]["exchange_ts"]) \
            > skew_ms:
        return None, "BBO_SKEW"

    payload = {
        "version": 2,
        "near": slots["near"],
        "far": slots["far"],
        "reconciliation_id": identity["reconciliation_id"],
        "snapshot_hash": identity["snapshot_hash"],
        "config_hash": identity["config_hash"],
        "release_sha": identity["release_sha"],
        "session_id": identity["session_id"],
    }
    bbo_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    captured_at = max(slots["near"]["exchange_ts"],
                      slots["far"]["exchange_ts"])
    return {"bbo_hash": bbo_hash, "bbo_captured_at": captured_at,
            "bbo_payload": payload}, None


def _json_safe(value):
    """[S2 audit] JSON-safe deep-copy: datetime -> isoformat, other
    non-serializable -> str; never raises."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            pass
    try:
        return str(value)
    except Exception:
        return "<unserializable>"


def build_bbo_failure_evidence(bbo_slots: Any, identity: Any,
                               reason: str) -> dict:
    """[S2 audit] canonical failure-evidence payload for every EXIT_ONLY
    ORDER_INTENT_BLOCKED: version bbo_input_v2, JSON-safe raw near/far
    slots, cap identity fields, reason and a deterministic hash of that
    payload (dashboard/review can reproduce the rejection).  Never
    writes secrets."""
    payload = {"version": "bbo_input_v2",
               "near": {}, "far": {},
               "reconciliation_id": None, "snapshot_hash": None,
               "config_hash": None, "release_sha": None,
               "session_id": None, "reason": reason}
    if isinstance(identity, dict):
        for _k in ("reconciliation_id", "snapshot_hash", "config_hash",
                   "release_sha", "session_id"):
            payload[_k] = identity.get(_k)
    if isinstance(bbo_slots, dict):
        for _leg in ("near", "far"):
            _slot = bbo_slots.get(_leg)
            if isinstance(_slot, dict):
                payload[_leg] = _json_safe(
                    {k: _slot.get(k) for k in
                     ("code", "bid", "ask", "exchange_ts_ms",
                      "received_at_ms", "source", "seq") if k in _slot})
    try:
        _canon = json.dumps(payload, sort_keys=True,
                            separators=(",", ":")).encode()
        payload["evidence_hash"] = hashlib.sha256(_canon).hexdigest()
    except Exception:
        payload["evidence_hash"] = None
    return payload


def attach_decision_binding(event: dict, capability: dict,
                            binding: dict) -> dict:
    """Return a copy of the decision event bound to capability + BBO —
    the raw versioned payload rides along (not just the hash) so the
    dashboard/review can reproduce the decision evidence."""
    merged = dict(event)
    merged["reconciliation_id"] = capability.get("reconciliation_id")
    merged["position_snapshot_hash"] = capability.get("snapshot_hash")
    merged["bbo_hash"] = binding.get("bbo_hash")
    merged["bbo_captured_at"] = binding.get("bbo_captured_at")
    merged["bbo_payload"] = binding.get("bbo_payload")
    return merged
