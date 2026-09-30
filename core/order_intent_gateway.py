"""S0: single MTS OrderIntentGateway authorization boundary (in-memory).

Process-local GatewayAuthorization (NOT a bearer string): bound to
{intent_id, execution_attempt, session_generation, process_epoch, expiry};
single-use; dies on restart.  The adapter verifies through the injected
gateway registry, bound to the exact order being submitted; direct adapter
calls (or calls for a different order than the pending authorization)
are rejected.  Only the authorization fingerprint is ever persistable.

Policy (merged from the signal-level gates): paper passes through
unchanged; LIVE_READY is the only order-capable live state.  Existing
positions block entries, while standard exits still require the same base
identity/route/session gateway checks.  There is no exit-only capability
branch.

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

from core.exit_only_position import ENTRY_ACTIONS

AUTH_TTL_S: float = 30.0

PENDING_SUBMIT = "PENDING_SUBMIT"
SUBMITTED = "SUBMITTED"
PENDING_RECONCILE = "PENDING_RECONCILE"
REJECTED = "REJECTED"
FILLED = "FILLED"

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

    # ── merged policy (P0 live / entry / standard exit / FLAT) ───────────

    def authorize_intent(self, *, action: str, strategy: Any,
                         authority: dict) -> tuple:
        """(ok, binding, reason).

        Reconciled holdings are ordinary ``LIVE_READY`` holdings.  The
        gateway remains the sole submit boundary: identity, route and
        authenticated-session failures are terminal for both entries and
        exits.  Position/pending/startup checks are entry-specific.
        """
        if not authority.get("live"):
            return True, None, None
        sname = strategy if isinstance(strategy, str) \
            else getattr(strategy, "strategy", "")
        if sname == "MTS_RELEASE_OCO":
            return False, None, "GATEWAY_OCO_DISABLED"
        mode = authority.get("mode")
        # Identity, route, and authenticated session are universal gates;
        # scoped reconciled exits must not bypass them.
        for field, reason in (("identity_ok", "GATEWAY_IDENTITY_FAILED"),
                              ("route_ok", "GATEWAY_ROUTE_FAILED"),
                              ("session_ok", "GATEWAY_SESSION_FAILED")):
            if field in authority and authority[field] is not True:
                return False, None, reason
        # Collapse 2: a broker-reconciled position has a scoped exit
        # authorization published by the pre-evaluation hydration
        # transition.  It must not be upgraded to NORMAL_LIVE and the
        # gateway must not hydrate capability/attestation a second time.
        if mode == "reconciled_exit_only":
            if action in ENTRY_ACTIONS:
                return False, None, "EXIT_ONLY_ENTRY_BLOCKED"
            if action not in EXIT_STRATEGIES and action not in ("EXIT", "PARTIAL_EXIT"):
                return False, None, "EXIT_ONLY_STRATEGY_BLOCKED"
            if not authority.get("reconciled_position_hydrated"):
                # A position may already be hydrated while the atomic
                # authorization publication is absent/stale.  Preserve the
                # established authorization/reconciliation reason there;
                # use HYDRATION_REQUIRED only when no position was hydrated.
                return False, None, (
                    "EXIT_ONLY_HYDRATION_REQUIRED"
                    if authority.get("hydrated_position") is None
                    else "LIVE_ORDER_AUTHORIZATION_FAILED")
            if not authority.get("exit_authorized"):
                return False, None, "LIVE_ORDER_AUTHORIZATION_FAILED"
            if not authority.get("position_has_position"):
                return False, None, "EXIT_FLAT_BLOCKED"
            return True, None, None
        if mode != "live_ready" or not authority.get("live_order_allowed"):
            return False, None, "LIVE_ORDER_AUTHORIZATION_FAILED"
        if action in ENTRY_ACTIONS:
            if authority.get("startup_not_ready"):
                return False, None, "STARTUP_NOT_READY"
            if authority.get("pending_order"):
                return False, None, "PENDING_ORDER_BLOCKED"
            if authority.get("position_has_position"):
                return False, None, "OPEN_POSITION_BLOCKED"
        elif action in ("EXIT", "PARTIAL_EXIT") \
                and not authority.get("position_has_position"):
            return False, None, "EXIT_FLAT_BLOCKED"
        return True, None, None

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
            # canonical broker identity only: a non-string (e.g. a mock
            # attribute in tests) is never a legitimate exchange identity
            _boid = getattr(order, "exchange_order_id", None)
            if not isinstance(_boid, str) or not _boid:
                _boid = None
            return {
                "order_id": getattr(order, "order_id", None),
                "broker_order_id": _boid,
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

    def reconcile_confirmed_fill(self, intent_id: str, *, broker_order_id: str,
                                 deal_id: str, symbol: str, side: str,
                                 quantity: int) -> bool:
        """Terminalize an exit from its confirmed full broker receipt only.

        This never issues authorization, resubmits, or enables new entries.
        Historical intents have no symbol/side/quantity bindings; their
        caller must be the already matched OrderManager full-fill callsite.
        """
        rec = self._intents.get(intent_id)
        if (rec is None or rec.get('strategy') not in EXIT_STRATEGIES
                or not isinstance(broker_order_id, str) or not broker_order_id
                or not isinstance(deal_id, str) or not deal_id
                or not isinstance(symbol, str) or not symbol
                or side not in ('buy', 'sell')
                or type(quantity) is not int or quantity <= 0):
            return False
        for field, value in (('broker_order_id', broker_order_id), ('symbol', symbol),
                             ('side', side), ('quantity', quantity)):
            if rec.get(field) is not None and rec[field] != value:
                return False
        if rec['state'] == FILLED:
            return rec.get('broker_deal_id') == deal_id
        if rec['state'] not in (SUBMITTED, PENDING_RECONCILE, PENDING_SUBMIT):
            return False
        previous = dict(rec)
        rec.update(state=FILLED, broker_order_id=broker_order_id,
                   broker_deal_id=deal_id, symbol=symbol, side=side,
                   quantity=quantity, reconciliation_id='broker-fill:' + deal_id)
        if not self._record(intent_id):
            self._intents[intent_id] = previous
            return False
        return True
