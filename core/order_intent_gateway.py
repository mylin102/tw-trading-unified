"""S0: single MTS OrderIntentGateway authorization boundary (in-memory).

Process-local GatewayAuthorization (NOT a bearer string): bound to
{intent_id, execution_attempt, session_generation, process_epoch, expiry};
single-use; dies on restart.  The adapter verifies through the injected
gateway registry; direct adapter calls without authorization fail.
Only the authorization fingerprint is ever persistable; token material
never leaves the process.

Policy (merged from the signal-level gates): paper passes through
unchanged; LIVE_READY behaves as before (state-file FLAT check merged);
RECONCILED_EXIT_ONLY admits only exact capability-bound MTS_EXIT /
MTS_RELEASE (subject to OrderManager/adapter defense); entry / manual /
generic / OCO are denied; OCO is explicitly rejected at the gateway.

PENDING_SUBMIT rule: the intent is durably represented (order_mgr order)
before the adapter submission; a missing broker receipt marks
PENDING_RECONCILE and the intent never resubmits automatically.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from core.exit_only_position import ENTRY_ACTIONS, build_bbo_binding

AUTH_TTL_S: float = 30.0

PENDING_SUBMIT = "PENDING_SUBMIT"
SUBMITTED = "SUBMITTED"
PENDING_RECONCILE = "PENDING_RECONCILE"

EXIT_STRATEGIES: frozenset = frozenset({"MTS_EXIT", "MTS_RELEASE"})


@dataclass(frozen=True)
class GatewayAuthorization:
    """Opaque process-local authorization. Never serialized as material."""

    intent_id: str
    execution_attempt: int
    session_generation: str
    process_epoch: str
    expiry_ts: float

    @property
    def fingerprint(self) -> str:
        """Only this hash may be persisted (audit correlation)."""
        return hashlib.sha256(json.dumps({
            "intent_id": self.intent_id,
            "execution_attempt": self.execution_attempt,
            "session_generation": self.session_generation,
            "process_epoch": self.process_epoch,
            "expiry_ts": self.expiry_ts,
        }, sort_keys=True).encode("utf-8")).hexdigest()


class GatewayAuthorizationRegistry:
    """Process-local; single-use; dies on restart (fresh registry).

    The adapter is injected with this registry and verifies the pending
    submission before any place call.  Direct adapter calls have no
    pending authorization and fail.
    """

    def __init__(self, process_epoch: Optional[str] = None):
        self._process_epoch = process_epoch or f"p{os.getpid()}"
        self._live: dict = {}
        self._pending: Optional[GatewayAuthorization] = None
        self._consumed: set = set()

    def issue(self, intent_id: str, execution_attempt: int,
              session_generation: str = "",
              expiry_ts: Optional[float] = None) -> GatewayAuthorization:
        auth = GatewayAuthorization(
            intent_id=intent_id,
            execution_attempt=execution_attempt,
            session_generation=session_generation,
            process_epoch=self._process_epoch,
            expiry_ts=expiry_ts or time.time() + AUTH_TTL_S,
        )
        self._live[intent_id] = auth
        self._pending = auth
        return auth

    def verify(self, auth: Any) -> bool:
        if not isinstance(auth, GatewayAuthorization):
            return False
        live = self._live.get(auth.intent_id)
        if live is None or live.fingerprint != auth.fingerprint:
            return False
        if auth.intent_id in self._consumed:
            return False
        if time.time() > auth.expiry_ts:
            return False
        return True

    def verify_pending_submission(self) -> bool:
        return self._pending is not None and self.verify(self._pending)

    def consume(self, auth: Any) -> bool:
        if not self.verify(auth):
            return False
        self._consumed.add(auth.intent_id)
        self._pending = None
        return True

    def invalidate(self, intent_id: str) -> None:
        self._live.pop(intent_id, None)
        self._consumed.discard(intent_id)

    @property
    def process_epoch(self) -> str:
        return self._process_epoch


class OrderIntentGateway:
    """In-memory intent ledger + merged policy + submission authorization."""

    def __init__(self, registry: Optional[GatewayAuthorizationRegistry] = None,
                 process_epoch: Optional[str] = None):
        self._registry = registry or GatewayAuthorizationRegistry(process_epoch)
        self._intents: dict = {}

    @property
    def registry(self) -> GatewayAuthorizationRegistry:
        return self._registry

    # ── merged policy (P0 live / entry / EXIT_ONLY / FLAT) ────────────────

    def authorize_intent(self, *, action: str, strategy: Any,
                         authority: dict) -> tuple:
        """(ok, binding, reason).  Paper passes through unchanged."""
        if not authority.get("live"):
            return True, None, None
        sname = strategy if isinstance(strategy, str) \
            else getattr(strategy, "strategy", "")
        if sname == "MTS_RELEASE_OCO":
            return False, None, "GATEWAY_OCO_DISABLED"
        mode = authority.get("mode")
        if mode != "reconciled_exit_only":
            if (mode == "live_ready"
                    and authority.get("live_order_allowed") is True):
                if action in ("EXIT", "PARTIAL_EXIT") \
                        and not authority.get("position_has_position"):
                    return False, None, "EXIT_FLAT_BLOCKED"
                return True, None, None
            return False, None, "LIVE_ORDER_AUTHORIZATION_FAILED"
        # EXIT_ONLY: exact capability-bound closing orders only
        if action in ENTRY_ACTIONS:
            return False, None, "EXIT_ONLY_ENTRY_BLOCKED"
        if sname not in EXIT_STRATEGIES:
            return False, None, "EXIT_ONLY_STRATEGY_BLOCKED"
        if authority.get("hydrated_position") is None:
            return False, None, "EXIT_ONLY_POSITION_MISSING"
        cap = authority.get("capability")
        if (not isinstance(cap, dict)
                or authority.get("strategy_reconciliation_id")
                != cap.get("reconciliation_id")):
            return False, None, "EXIT_ONLY_STRATEGY_BLOCKED"
        binding, reason = build_bbo_binding(authority.get("bbo_slots") or {})
        if binding is None:
            return False, None, reason
        legs = cap.get("legs") or []
        if len(legs) == 2:
            near_code = authority.get("near_code")
            far_code = authority.get("far_code")
            if legs[0].get("symbol") != near_code \
                    or legs[1].get("symbol") != far_code:
                return False, None, "BBO_CODE_MISMATCH"
        return True, binding, None

    # ── submission (authorization + receipt rule) ─────────────────────────

    def submit_with_authorization(
            self, order: Any, *, mode: str = "live",
            session_generation: str = "", exchange_ordno: Optional[str] = None,
            submit_callable: Optional[Callable] = None) -> tuple:
        """Record the intent durably, issue a single-use authorization and
        submit.  Receipt missing -> PENDING_RECONCILE, never auto-resubmit.

        Paper (mode != live or exchange_ordno) -> direct submit unchanged.
        Returns (ok, reason_or_empty).
        """
        intent_id = getattr(order, "order_id", None)
        if not intent_id or submit_callable is None:
            return False, "GATEWAY_INTENT_MISSING"
        rec = self._intents.setdefault(intent_id, {
            "state": PENDING_SUBMIT,
            "execution_attempt": 0,
            "strategy": getattr(order, "strategy", ""),
            "reconciliation_id": getattr(order, "reconciliation_id", None),
            "session_generation": session_generation,
        })
        if rec["state"] == PENDING_RECONCILE:
            return False, "GATEWAY_RECONCILE_REQUIRED"
        if mode != "live" or exchange_ordno is not None:
            rec["state"] = SUBMITTED
            if exchange_ordno is not None:
                ok = bool(submit_callable(order, exchange_ordno=exchange_ordno))
            else:
                ok = bool(submit_callable(order))
            return ok, ("" if ok else "SUBMIT_FAILED")
        rec["execution_attempt"] += 1
        auth = self._registry.issue(
            intent_id, rec["execution_attempt"], session_generation)
        try:
            ok = bool(submit_callable(order))
        finally:
            self._registry.consume(auth)
        if ok and getattr(order, "exchange_order_id", None):
            rec["state"] = SUBMITTED
            return True, ""
        rec["state"] = PENDING_RECONCILE
        return False, "GATEWAY_RECEIPT_MISSING_RECONCILE"

    def intent_state(self, intent_id: str) -> Optional[str]:
        rec = self._intents.get(intent_id)
        return rec["state"] if rec else None
