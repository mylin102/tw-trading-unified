"""Fail-closed operator attestation for one reconciled futures spread.

This module never grants general LIVE_READY.  It creates a restart-persisted
capability for exactly the two broker-reconciled closing orders described by a
fresh snapshot and an explicit, non-secret operator attestation.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from dataclasses import replace
from datetime import datetime
from typing import Any

from core.mode_transition import ExecutionContext, ModeTransitionState

SNAPSHOT_TTL_MS = 60_000
_HEX_32 = re.compile(r"^[0-9a-f]{32}$")
_HEX_40 = re.compile(r"^[0-9a-f]{40}$")
_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PATTERNS = (
    "PASSWORD", "SECRET", "PRIVATE KEY", "BEGIN CERTIFICATE",
    "SHIOAJI_", "API_KEY", "TOKEN",
)


class AttestationError(Exception):
    """Typed, operator-safe attestation failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _closing_side(open_side: str) -> str:
    return "buy" if open_side == "sell" else "sell"


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _parse_attested_at(value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise AttestationError("ATTESTATION_INVALID", "attested_at required")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttestationError("ATTESTATION_INVALID", "attested_at invalid") from exc
    return value


def _require_context(ctx: ExecutionContext | None) -> dict:
    if not isinstance(ctx, ExecutionContext):
        raise AttestationError("EXIT_ONLY_CONTEXT_INVALID", "context required")
    # Normal entry certification correctly refuses a non-flat account.  That
    # must not make a manually reconciled position impossible to close.  The
    # *only* non-LIVE_READY state accepted here is a live quarantine whose
    # recorded reason is the broker-position gate; every other quarantine
    # remains a hard stop (release identity, session, margin, etc.).
    if not ctx.is_live_ready():
        reasons = set(ctx.audit_reasons or ())
        position_quarantine = (
            ctx.requested_mode == "live"
            and ctx.effective_mode == ModeTransitionState.LIVE_QUARANTINED.value
            and not ctx.live_order_allowed
            and bool(reasons & {"BROKER_NOT_FLAT", "GUARD_POSITION_NOT_FLAT"})
            and ctx.exit_only_capability is None
        )
        if not position_quarantine:
            raise AttestationError(
                "EXIT_ONLY_CONTEXT_INVALID", "context not reconcilable")
    account = ctx.account_id_hash or ""
    session = ctx.session_id or ""
    config = ctx.config_hash or ""
    if not _HEX_64.fullmatch(account) or not _HEX_32.fullmatch(session) or not _HEX_64.fullmatch(config):
        raise AttestationError("EXIT_ONLY_CONTEXT_INVALID", "bound live identity missing")
    return {"account_id_hash": account, "session_id": session,
            "config_hash": config}


def _normalize_expected_legs(value: Any) -> list[dict]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise AttestationError("ATTESTATION_INVALID", "exactly two expected legs required")
    result: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for leg in value:
        if not isinstance(leg, dict):
            raise AttestationError("ATTESTATION_INVALID", "expected leg must be an object")
        symbol = leg.get("symbol")
        side = str(leg.get("side", "")).lower()
        quantity = _positive_int(leg.get("remaining_qty"))
        if not isinstance(symbol, str) or not symbol or side not in {"buy", "sell"} or quantity is None:
            raise AttestationError("ATTESTATION_INVALID", "expected leg invalid")
        key = (symbol, side)
        if key in seen:
            raise AttestationError("ATTESTATION_INVALID", "duplicate expected leg")
        seen.add(key)
        result.append({"symbol": symbol, "side": side, "remaining_qty": quantity})
    return sorted(result, key=lambda leg: (leg["symbol"], leg["side"]))


def _normalize_positions(value: Any) -> list[dict]:
    if not isinstance(value, list):
        raise AttestationError("SNAPSHOT_LEG_MISMATCH", "positions must be a list")
    result: list[dict] = []
    for position in value:
        if not isinstance(position, dict):
            raise AttestationError("SNAPSHOT_LEG_MISMATCH", "position must be an object")
        code = position.get("code")
        direction = str(position.get("direction", "")).lower()
        quantity = _positive_int(position.get("quantity"))
        try:
            avg_cost = float(position.get("avg_cost"))
        except (TypeError, ValueError):
            avg_cost = float("nan")
        if (not isinstance(code, str) or not code or direction not in {"buy", "sell"}
                or quantity is None or not math.isfinite(avg_cost) or avg_cost <= 0):
            raise AttestationError("SNAPSHOT_LEG_MISMATCH", "position invalid")
        result.append({"code": code, "direction": direction,
                       "quantity": quantity, "avg_cost": avg_cost})
    return sorted(result, key=lambda item: (item["code"], item["direction"]))


def build_exit_only_capability(attestation: dict | None, snapshot: dict | None,
                               *, ctx: ExecutionContext | None) -> tuple[dict, dict]:
    """Validate all evidence and return an exact two-order capability.

    The caller must pass the active, registry-bound LIVE_READY context.  The
    snapshot repeats its non-secret identities and must match it exactly;
    independently captured or stale evidence cannot authorize an exit.
    """
    identity = _require_context(ctx)
    if not isinstance(attestation, dict):
        raise AttestationError("ATTESTATION_INVALID", "attestation must be an object")
    operator = attestation.get("operator")
    trade_id = attestation.get("trade_id")
    evidence = attestation.get("evidence")
    if not isinstance(operator, str) or not operator.strip() or not isinstance(trade_id, str) or not trade_id.strip():
        raise AttestationError("ATTESTATION_INVALID", "operator and trade_id required")
    if not isinstance(evidence, str) or not evidence.strip():
        raise AttestationError("ATTESTATION_INVALID", "evidence required")
    if any(pattern in evidence.upper() for pattern in _SECRET_PATTERNS):
        raise AttestationError("ATTESTATION_SECRET_REJECTED", "secret-like evidence")
    attested_at = _parse_attested_at(attestation.get("attested_at"))
    expected_legs = _normalize_expected_legs(attestation.get("expected_legs"))

    if isinstance(snapshot, dict) and snapshot.get("capture_error"):
        raise AttestationError("BROKER_SNAPSHOT_UNAVAILABLE", "broker capture failed")
    if not isinstance(snapshot, dict) or snapshot.get("source") != "live_broker":
        raise AttestationError("SNAPSHOT_SOURCE_INVALID", "live_broker source required")
    captured_at = snapshot.get("captured_at")
    now = int(time.time() * 1000)
    if (not isinstance(captured_at, int) or isinstance(captured_at, bool)
            or captured_at > now + 1_000 or now - captured_at > SNAPSHOT_TTL_MS):
        raise AttestationError("SNAPSHOT_STALE", "snapshot timestamp invalid or stale")
    for field, expected in identity.items():
        if snapshot.get(field) != expected:
            raise AttestationError("SNAPSHOT_IDENTITY_MISMATCH", field)
    release_sha = snapshot.get("release_sha")
    if not isinstance(release_sha, str) or not _HEX_40.fullmatch(release_sha):
        raise AttestationError("SNAPSHOT_IDENTITY_MISMATCH", "release_sha")
    open_orders = snapshot.get("open_orders")
    if not isinstance(open_orders, list):
        raise AttestationError("OPEN_ORDERS_NOT_EMPTY", "open_orders invalid")
    if open_orders:
        raise AttestationError("OPEN_ORDERS_NOT_EMPTY", "open orders exist")
    positions = _normalize_positions(snapshot.get("positions"))
    if len(positions) != 2 or len(positions) != len(expected_legs):
        raise AttestationError("SNAPSHOT_LEG_MISMATCH", "unexpected position count")
    costs: dict[tuple[str, str], float] = {}
    for leg in expected_legs:
        matches = [p for p in positions if (p["code"], p["direction"], p["quantity"])
                   == (leg["symbol"], leg["side"], leg["remaining_qty"])]
        if len(matches) != 1:
            raise AttestationError("SNAPSHOT_LEG_MISMATCH", "expected leg not unique")
        costs[(leg["symbol"], leg["side"])] = matches[0]["avg_cost"]

    snapshot_payload = {
        "version": 2, "source": "live_broker", "captured_at": captured_at,
        **identity, "release_sha": release_sha, "positions": positions,
        "open_orders": [],
    }
    snapshot_hash = _hash(snapshot_payload)
    attestation_payload = {
        "version": 1, "operator": operator.strip(), "attested_at": attested_at,
        "trade_id": trade_id.strip(), "evidence": evidence.strip(),
        "expected_legs": expected_legs,
    }
    attestation_hash = _hash(attestation_payload)
    reconciliation_id = _hash({"version": 1, "snapshot_hash": snapshot_hash,
                               "attestation_hash": attestation_hash})
    allowed_orders = [{"symbol": leg["symbol"], "side": _closing_side(leg["side"]),
                       "remaining_qty": leg["remaining_qty"]}
                      for leg in expected_legs]
    capability = {
        "schema_version": 2, "reconciliation_id": reconciliation_id,
        "trade_id": trade_id.strip(), "snapshot_hash": snapshot_hash,
        "attestation_hash": attestation_hash, "snapshot_captured_at": captured_at,
        **identity, "release_sha": release_sha, "allowed_orders": allowed_orders,
    }
    record = {
        **attestation_payload, "source": "live_broker", "snapshot_hash": snapshot_hash,
        "snapshot_captured_at": captured_at, **identity, "release_sha": release_sha,
        "legs": [{"symbol": leg["symbol"], "side": leg["side"],
                  "quantity": leg["remaining_qty"],
                  "avg_cost": costs[(leg["symbol"], leg["side"])]}
                 for leg in expected_legs],
    }
    return capability, record


def apply_exit_only(ctx: ExecutionContext, capability: dict) -> ExecutionContext:
    """Switch a verified LIVE_READY context to distinct exit-only mode."""
    identity = _require_context(ctx)
    if not isinstance(capability, dict) or any(capability.get(k) != v for k, v in identity.items()):
        raise AttestationError("EXIT_ONLY_CONTEXT_INVALID", "capability/context mismatch")
    return replace(ctx, effective_mode=ModeTransitionState.RECONCILED_EXIT_ONLY.value,
                   live_order_allowed=False, exit_only_capability=dict(capability))


def _order_value(order: Any, field: str) -> Any:
    value = getattr(order, field, None)
    return getattr(value, "value", value)


def capability_exit_completed(ctx: ExecutionContext, orders: list[Any]) -> bool:
    """True only when each exact capability closing leg is FILLED once.

    This intentionally does *not* alter mode or grant normal live trading.
    It is only the local callback half of the post-exit proof.
    """
    cap = getattr(ctx, "exit_only_capability", None)
    if (getattr(ctx, "effective_mode", None)
            != ModeTransitionState.RECONCILED_EXIT_ONLY.value
            or not isinstance(cap, dict) or not isinstance(orders, list)):
        return False
    allowed = cap.get("allowed_orders")
    if not isinstance(allowed, list) or len(allowed) != 2:
        return False
    matching = [order for order in orders
                if getattr(order, "reconciliation_id", None)
                == cap.get("reconciliation_id")]
    if len(matching) != 2:
        return False
    for item in allowed:
        leg = [order for order in matching
               if _order_value(order, "symbol") == item.get("symbol")
               and str(_order_value(order, "side")).lower()
               == str(item.get("side", "")).lower()
               and _order_value(order, "quantity") == item.get("remaining_qty")]
        if len(leg) != 1 or str(_order_value(leg[0], "status")).lower() != "filled":
            return False
    return True


def revoke_exit_only_after_flat_snapshot(
        ctx: ExecutionContext, snapshot: dict | None) -> tuple[ExecutionContext, dict]:
    """Revoke one exit-only capability after same-session broker-flat proof.

    The result is deliberately LIVE_QUARANTINED, not LIVE_READY.  A separate
    fresh normal certificate is still required before MTS entries resume.
    """
    cap = getattr(ctx, "exit_only_capability", None)
    if (not isinstance(ctx, ExecutionContext)
            or ctx.effective_mode != ModeTransitionState.RECONCILED_EXIT_ONLY.value
            or not isinstance(cap, dict)):
        raise AttestationError("EXIT_ONLY_CAPABILITY_MISSING", "exit-only capability required")
    if isinstance(snapshot, dict) and snapshot.get("capture_error"):
        raise AttestationError("BROKER_SNAPSHOT_UNAVAILABLE", "broker capture failed")
    if not isinstance(snapshot, dict) or snapshot.get("source") != "live_broker":
        raise AttestationError("SNAPSHOT_SOURCE_INVALID", "live_broker source required")
    captured_at = snapshot.get("captured_at")
    now = int(time.time() * 1000)
    if (not isinstance(captured_at, int) or isinstance(captured_at, bool)
            or captured_at > now + 1_000 or now - captured_at > SNAPSHOT_TTL_MS):
        raise AttestationError("SNAPSHOT_STALE", "post-exit snapshot stale")
    for field in ("account_id_hash", "session_id", "config_hash", "release_sha"):
        if snapshot.get(field) != cap.get(field):
            raise AttestationError("SNAPSHOT_IDENTITY_MISMATCH", field)
    if snapshot.get("positions") != []:
        raise AttestationError("EXIT_ONLY_POSITION_NOT_FLAT", "broker still reports positions")
    if snapshot.get("open_orders") != []:
        raise AttestationError("OPEN_ORDERS_NOT_EMPTY", "broker still reports open orders")
    flat_payload = {
        "version": 2, "source": "live_broker", "captured_at": captured_at,
        "account_id_hash": snapshot["account_id_hash"],
        "session_id": snapshot["session_id"],
        "config_hash": snapshot["config_hash"],
        "release_sha": snapshot["release_sha"],
        "positions": [], "open_orders": [],
    }
    record = {
        "reconciliation_id": cap.get("reconciliation_id"),
        "trade_id": cap.get("trade_id"),
        "entry_snapshot_hash": cap.get("snapshot_hash"),
        "snapshot_hash": _hash(flat_payload),
        "snapshot_captured_at": captured_at,
        "source": "live_broker",
    }
    return replace(ctx,
                   effective_mode=ModeTransitionState.LIVE_QUARANTINED.value,
                   live_order_allowed=False,
                   exit_only_capability=None,
                   audit_reasons=("EXIT_ONLY_FLAT_RECONCILED",)), record
