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
        if mode == "live_quarantined":
            # [QUARANTINE_EXIT 2026-08-18] narrow carve-out: only a
            # snapshot-bound remaining-leg exit (per-order proof from
            # the monitor's fresh broker-truth recheck) may submit.
            _qproof = authority.get("quarantine_exit_proof")
            # strict proof values (follow-up audit): side LONG/SHORT only
            # (no fall-through to a default close side), qty/order_qty
            # positive int non-bool, order_side BUY/SELL.
            _q_side = _qproof.get("side") if isinstance(_qproof, dict) else None
            _q_qty = _qproof.get("qty") if isinstance(_qproof, dict) else None
            _q_order_qty = (_qproof.get("order_qty")
                            if isinstance(_qproof, dict) else None)
            _q_order_side = (_qproof.get("order_side")
                             if isinstance(_qproof, dict) else None)
            _close_side = ("SELL" if _q_side == "LONG"
                           else "BUY") if _q_side in ("LONG", "SHORT") else None
            _strict_qty = (isinstance(_q_qty, int)
                           and not isinstance(_q_qty, bool) and _q_qty > 0)
            _strict_oqty = (isinstance(_q_order_qty, int)
                            and not isinstance(_q_order_qty, bool)
                            and _q_order_qty > 0)
            # order-spec coherence: the bound order closes the remaining
            # leg (order_symbol == contract, closing side, qty match).
            if (isinstance(_qproof, dict)
                    and sname in ("MTS_RELEASE", "MTS_EXIT")
                    and _qproof.get("strategy") == sname
                    and _q_side in ("LONG", "SHORT")
                    and str(_q_order_side or "").upper() in ("BUY", "SELL")
                    and _strict_qty and _strict_oqty
                    and _qproof.get("contract") and _qproof.get("snapshot_hash")
                    and _qproof.get("order_symbol") == _qproof.get("contract")
                    and str(_q_order_side or "").lower()
                        == str(_close_side or "").lower()
                    and _q_order_qty == _q_qty):
                return True, _qproof, None
            return False, None, "LIVE_ORDER_AUTHORIZATION_FAILED"
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
        _bbo_slots = authority.get("bbo_slots") or {}
        _identity = {
            "reconciliation_id": cap.get("reconciliation_id"),
            "snapshot_hash": cap.get("snapshot_hash"),
            "config_hash": cap.get("config_hash"),
            "release_sha": cap.get("release_sha"),
            "session_id": cap.get("session_id"),
        }
        binding, reason = build_bbo_binding(
            _bbo_slots, near_code=authority.get("near_code"),
            far_code=authority.get("far_code"), identity=_identity)
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
