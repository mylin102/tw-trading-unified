"""S0: single MTS OrderIntentGateway authorization boundary (in-memory).

Process-local GatewayAuthorization (NOT a bearer string): bound to
{intent_id, execution_attempt, session_generation, process_epoch, expiry};
single-use; dies on restart.  The adapter verifies through the injected
gateway registry, bound to the exact order being submitted; direct adapter
calls (or calls for a different order than the pending authorization)
are rejected.  Only the authorization fingerprint is ever persistable.

Policy (merged from the signal-level gates): paper passes through
unchanged; LIVE_READY behaves as before (state-file FLAT check merged);
every other LIVE mode (LIVE_QUARANTINED etc.) is explicitly denied —
never reclassified as paper; RECONCILED_EXIT_ONLY admits only exact
capability-bound MTS_EXIT / MTS_RELEASE (subject to OrderManager/adapter
defense); entry / manual / generic / OCO are denied; OCO is explicitly
rejected at the gateway.

PENDING_SUBMIT rule: the intent is durably recorded (record_cb -> the
execution-context payload) BEFORE the adapter submission; a missing broker
receipt marks PENDING_RECONCILE (a local rejection marks REJECTED) and
the intent never resubmits automatically — a fresh gateway (restart)
replays the durable intents and refuses resubmission (GATEWAY_RECONCILE_REQUIRED).
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
REJECTED = "REJECTED"

EXIT_STRATEGIES: frozenset = frozenset({"MTS_EXIT", "MTS_RELEASE"})


class GatewaySubmitError(Exception):
    """Raised by _submit_via_gateway(raise_on_failure=True): the exit-intent
    submit_leg contract requires a failed leg to never be marked SUBMITTED."""


class GatewayIntentPersistFailed(Exception):
    """Typed persistence failure: the durable PENDING_SUBMIT was not
    confirmed, so no authorization may be issued and no submit may run."""


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
    submission — bound to the exact order_id — before any place call.
    Direct adapter calls, or calls for a different order than the pending
    authorization, fail.
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

    def verify_pending_submission(self, order: Any = None) -> bool:
        """Pending authorization must be live AND bound to the exact order."""
        if self._pending is None or not self.verify(self._pending):
            return False
        if order is None:
            return False
        oid = getattr(order, "order_id", None)
        if oid != self._pending.intent_id:
            return False
        return True

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
    """In-memory intent ledger + merged policy + submission authorization.

    ``durable_intents`` replays the persisted intent ledger (restart
    recovery); every state change is pushed to ``record_cb`` so the caller
    can persist it durably (execution-context payload).
    """

    def __init__(self, registry: Optional[GatewayAuthorizationRegistry] = None,
                 process_epoch: Optional[str] = None,
                 durable_intents: Optional[dict] = None,
                 record_cb: Optional[Callable] = None):
        self._registry = registry or GatewayAuthorizationRegistry(process_epoch)
        self._intents: dict = {}
        self._record_cb = record_cb
        for iid, rec in (durable_intents or {}).items():
            _rec = dict(rec)
            _rec["_durable"] = True
            self._intents[iid] = _rec

    @property
    def registry(self) -> GatewayAuthorizationRegistry:
        return self._registry

    def durable_view(self) -> dict:
        """The persisted-safe ledger (internal flags excluded)."""
        out = {}
        for iid, rec in self._intents.items():
            out[iid] = {k: v for k, v in rec.items() if not k.startswith("_")}
        return out

    def _record(self, intent_id: str) -> bool:
        """Push the ledger to record_cb.  Returns success — a persistence
        failure must abort the submission (no auth issued, no adapter call)."""
        if self._record_cb is None:
            return True
        try:
            self._record_cb(self.durable_view())
            return True
        except Exception:
            return False

    # ── merged policy (P0 live / entry / EXIT_ONLY / FLAT) ────────────────

    def authorize_intent(self, *, action: str, strategy: Any,
                         authority: dict) -> tuple:
        """(ok, binding, reason).  True paper passes through unchanged;
        every live mode outside LIVE_READY / RECONCILED_EXIT_ONLY is
        explicitly denied (never reclassified as paper)."""
        if not authority.get("live"):
            return True, None, None
        sname = strategy if isinstance(strategy, str) \
            else getattr(strategy, "strategy", "")
        if sname == "MTS_RELEASE_OCO":
            return False, None, "GATEWAY_OCO_DISABLED"
        mode = authority.get("mode")
        if mode not in ("live_ready", "reconciled_exit_only"):
            return False, None, "LIVE_ORDER_AUTHORIZATION_FAILED"
        if mode == "live_ready":
            if action in ("EXIT", "PARTIAL_EXIT") \
                    and not authority.get("position_has_position"):
                return False, None, "EXIT_FLAT_BLOCKED"
            return True, None, None
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
        submit.  Returns (ok, payload): on success the canonical receipt
        dict; on failure the typed reason.  A local rejection marks REJECTED,
        a missing broker receipt PENDING_RECONCILE — never auto-resubmit.

        Paper (mode != live or exchange_ordno) -> direct submit unchanged.
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
        fresh = (not rec.get("_durable")
                 and rec["state"] == PENDING_SUBMIT
                 and rec["execution_attempt"] == 0)
        if not fresh:
            return False, "GATEWAY_RECONCILE_REQUIRED"

        def _receipt() -> dict:
            return {
                "order_id": order.order_id,
                "broker_order_id": getattr(order, "exchange_order_id", None),
            }

        if mode != "live" or exchange_ordno is not None:
            rec["state"] = SUBMITTED
            self._record(intent_id)
            if exchange_ordno is not None:
                ok = bool(submit_callable(order, exchange_ordno=exchange_ordno))
            else:
                ok = bool(submit_callable(order))
            if not ok:
                rec["state"] = REJECTED
                self._record(intent_id)
                return False, "SUBMIT_REJECTED"
            return True, _receipt()
        rec["execution_attempt"] += 1
        if not self._record(intent_id):
            # durable PENDING_SUBMIT must be confirmed BEFORE any adapter I/O
            return False, "GATEWAY_INTENT_PERSIST_FAILED"
        auth = self._registry.issue(
            intent_id, rec["execution_attempt"], session_generation)
        try:
            ok = bool(submit_callable(order))
        finally:
            self._registry.consume(auth)
        if not ok:
            rec["state"] = REJECTED
            self._record(intent_id)
            return False, "SUBMIT_REJECTED"
        if not getattr(order, "exchange_order_id", None):
            rec["state"] = PENDING_RECONCILE
            self._record(intent_id)
            return False, "GATEWAY_RECEIPT_MISSING_RECONCILE"
        rec["state"] = SUBMITTED
        self._record(intent_id)
        return True, _receipt()

    def intent_state(self, intent_id: str) -> Optional[str]:
        rec = self._intents.get(intent_id)
        return rec["state"] if rec else None
