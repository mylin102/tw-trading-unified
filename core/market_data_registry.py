#!/usr/bin/env python3
"""
Exact-contract market data registry.

Binds individual contract identities (exchange + code) to route objects
that specify a tick handler and leg (near/far).  Lookup is O(1).

.. important::
   This registry performs exact lookups only.  No implicit case folding,
   whitespace stripping, or exchange-code coercion is applied.
   All inputs must be canonicalized *before* calling ``bind_contract``
   or ``lookup`` (see ``GlobalCallbackAdapter.normalize_exchange`` and
   ``normalize_contract_code``).

Thread safety
=============
All public mutation and query methods are guarded by a reentrant lock
(``RLock``).  Callback threads calling ``lookup`` concurrently with
watchdog/rollover threads calling ``bind`` / ``unbind`` / ``clear``
are safe.  The lock is held only for the duration of the dict operation;
no I/O or long computation happens under the lock.
"""

from __future__ import annotations

from threading import RLock

from core.market_data_contracts import ContractIdentity, ContractRoute


class DuplicateContractBindingError(ValueError):
    """Raised when an attempt is made to bind an already-registered identity."""


class MarketDataRegistry:
    """Registry mapping ContractIdentity → ContractRoute.

    One identity maps to at most one route.  Duplicate bindings raise.
    Rollover callers must unbind the old identity before binding the new one.
    """

    def __init__(self) -> None:
        self._map: dict[ContractIdentity, ContractRoute] = {}
        self._lock = RLock()

    # ── Mutation ──

    def bind_contract(self, identity: ContractIdentity, route: ContractRoute) -> None:
        """Register a route for *identity*.

        Raises ``DuplicateContractBindingError`` if the identity is already bound.
        Callers must ``unbind_contract`` first during rollover.
        """
        with self._lock:
            if identity in self._map:
                raise DuplicateContractBindingError(
                    f"Contract already bound: "
                    f"exchange={identity.exchange!r}, "
                    f"code={identity.contract_code!r}"
                )
            self._map[identity] = route

    def unbind_contract(self, identity: ContractIdentity) -> None:
        """Remove a previously bound identity.  No-op if not bound."""
        with self._lock:
            self._map.pop(identity, None)

    def clear(self) -> None:
        """Remove all bindings."""
        with self._lock:
            self._map.clear()

    # ── Lookup ──

    def lookup(self, exchange: str, contract_code: str) -> ContractRoute | None:
        """Look up route by exchange + contract code.

        Returns None when no binding exists (caller should fall back to the
        existing TMF callback).
        """
        identity = ContractIdentity(exchange=exchange, contract_code=contract_code)
        with self._lock:
            return self._map.get(identity)

    # ── Inspection ──

    @property
    def binding_count(self) -> int:
        with self._lock:
            return len(self._map)

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.binding_count} bindings)"
