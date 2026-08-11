"""RECONCILED_EXIT_ONLY attestation flow (P0 completion).

Broker-automatic evidence (position-detail) is insufficient to uniquely
attribute a two-leg spread to a local trade_id (observed: TMFH6
dseq=va042 only, no far-month receipt).  Operator attestation is therefore
a HARD requirement: who/when/evidence, dashboard-displayable, no secrets.

Without a valid attestation AND a fresh matching broker snapshot the
system stays N/A + zero orders (the adapter gate is default-deny).

Contract (final review standard):
- distinct effective mode ``reconciled_exit_only`` (never LIVE_READY)
- capability binds reconciliation_id + exact contracts + CLOSING sides +
  remaining qty; every other order is rejected at the adapter
- new entries / manual entry / other trades / generic update-cancel are
  blocked; any partial fill / callback ambiguity / cancel / reconnect
  quarantines; zero automatic IOC retry/reissue
- decisions bind position snapshot canonical hash + BBO snapshot
- paper is never affected
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import replace
from typing import Any, Optional, Tuple

from core.mode_transition import ExecutionContext, ModeTransitionState

# Fresh broker snapshot window (ms).  A stale snapshot never authorizes.
SNAPSHOT_TTL_MS: int = 60_000

# Exit-class strategies that a RECONCILED_EXIT_ONLY capability may stamp.
EXIT_STRATEGIES: frozenset = frozenset(
    {"MTS_EXIT", "MTS_RELEASE", "MTS_RELEASE_OCO"})

# Never let operator-typed evidence carry secret material into events.
_SECRET_PATTERNS: tuple = (
    "PASSWORD", "SECRET", "PRIVATE KEY", "BEGIN CERTIFICATE",
    "SHIOAJI_", "API_KEY", "TOKEN",
)


class AttestationError(Exception):
    """Typed, fail-closed attestation failure (code is operator-safe)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


def _closing_side(open_side: str) -> str:
    return "buy" if str(open_side).lower() == "sell" else "sell"


def build_exit_only_capability(
    attestation: Optional[dict],
    snapshot: Optional[dict],
) -> Tuple[dict, dict]:
    """Validate operator attestation + fresh broker snapshot.

    Returns ``(capability, record)``:
      capability = {reconciliation_id, allowed_orders:[{symbol, side,
                     remaining_qty}]}  -- allowed_orders use CLOSING sides
      record     = dashboard-displayable attestation record (no secrets)

    Raises AttestationError(code, detail) on any failure (fail-closed).
    """
    if not isinstance(attestation, dict):
        raise AttestationError("ATTESTATION_INVALID",
                               "attestation must be an object")

    operator = attestation.get("operator", "")
    attested_at = attestation.get("attested_at", "")
    trade_id = attestation.get("trade_id", "")
    evidence = str(attestation.get("evidence", "") or "")
    expected_legs = attestation.get("expected_legs")

    if not isinstance(operator, str) or not operator.strip():
        raise AttestationError("ATTESTATION_INVALID", "operator required")
    if not isinstance(attested_at, str) or not attested_at:
        raise AttestationError("ATTESTATION_INVALID", "attested_at required")
    if not isinstance(trade_id, str) or not trade_id.strip():
        raise AttestationError("ATTESTATION_INVALID", "trade_id required")
    _upper = evidence.upper()
    if any(_p in _upper for _p in _SECRET_PATTERNS):
        raise AttestationError(
            "ATTESTATION_SECRET_REJECTED",
            "evidence may not contain secret material")

    if not isinstance(expected_legs, (list, tuple)) or len(expected_legs) != 2:
        raise AttestationError(
            "ATTESTATION_INVALID", "exactly two expected legs required")

    norm_legs = []
    for leg in expected_legs:
        if not isinstance(leg, dict):
            raise AttestationError("ATTESTATION_INVALID",
                                   "expected leg must be an object")
        symbol = leg.get("symbol")
        side = leg.get("side")
        qty = leg.get("remaining_qty")
        if not isinstance(symbol, str) or not symbol.strip():
            raise AttestationError("ATTESTATION_INVALID",
                                   "expected leg symbol required")
        if str(side).lower() not in ("buy", "sell"):
            raise AttestationError("ATTESTATION_INVALID",
                                   f"expected leg side invalid: {side}")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise AttestationError(
                "ATTESTATION_INVALID",
                "expected leg remaining_qty must be a positive int")
        norm_legs.append({
            "symbol": symbol,
            "side": str(side).lower(),
            "remaining_qty": qty,
        })

    # ── fresh live_broker snapshot ──
    if not isinstance(snapshot, dict) or snapshot.get("source") != "live_broker":
        raise AttestationError("SNAPSHOT_SOURCE_INVALID",
                               "snapshot must come from live_broker")
    captured_at = snapshot.get("captured_at")
    if not isinstance(captured_at, int) or isinstance(captured_at, bool):
        raise AttestationError("SNAPSHOT_STALE",
                               "captured_at missing/invalid")
    if int(time.time() * 1000) - captured_at > SNAPSHOT_TTL_MS:
        raise AttestationError("SNAPSHOT_STALE",
                               "broker snapshot too old")

    positions = snapshot.get("positions") or []
    open_orders = snapshot.get("open_orders") or []
    if open_orders:
        raise AttestationError("OPEN_ORDERS_NOT_EMPTY",
                               "open orders must be empty")

    if len(positions) != len(norm_legs):
        raise AttestationError(
            "SNAPSHOT_LEG_MISMATCH",
            f"positions count {len(positions)} != expected {len(norm_legs)}")

    costs: dict = {}
    for leg in norm_legs:
        matches = [
            p for p in positions
            if str(p.get("code", "")) == leg["symbol"]
            and str(p.get("direction", "")).lower() == leg["side"]
            and int(p.get("quantity", 0) or 0) == leg["remaining_qty"]
        ]
        if len(matches) != 1:
            raise AttestationError(
                "SNAPSHOT_LEG_MISMATCH",
                f"leg {leg['symbol']} {leg['side']} "
                f"x{leg['remaining_qty']} not uniquely matched")
        costs[leg["symbol"]] = float(matches[0].get("avg_cost") or 0)

    # ── canonical snapshot hash (versioned serialization) ──
    snapshot_hash = hashlib.sha256(
        json.dumps({
            "version": 1,
            "captured_at": captured_at,
            "positions": sorted(
                positions, key=lambda p: str(p.get("code", ""))),
            "open_orders": open_orders,
        }, sort_keys=True).encode()).hexdigest()

    capability = {
        "reconciliation_id": trade_id,
        "allowed_orders": [
            {"symbol": leg["symbol"],
             "side": _closing_side(leg["side"]),
             "remaining_qty": leg["remaining_qty"]}
            for leg in norm_legs
        ],
    }

    record = {
        "operator": operator.strip(),
        "attested_at": attested_at,
        "trade_id": trade_id,
        "evidence": evidence,
        "source": "live_broker",
        "snapshot_captured_at": captured_at,
        "snapshot_hash": snapshot_hash,
        "legs": [
            {"symbol": leg["symbol"], "side": leg["side"],
             "quantity": leg["remaining_qty"],
             "avg_cost": costs.get(leg["symbol"], 0.0)}
            for leg in norm_legs
        ],
    }
    return capability, record


def apply_exit_only(ctx: ExecutionContext, capability: dict) -> ExecutionContext:
    """Return a NEW ExecutionContext in RECONCILED_EXIT_ONLY.

    Never LIVE_READY; live_order_allowed is forced False.  The original
    context is unchanged (frozen dataclass).
    """
    return replace(
        ctx,
        effective_mode=ModeTransitionState.RECONCILED_EXIT_ONLY.value,
        live_order_allowed=False,
        exit_only_capability=dict(capability),
    )
