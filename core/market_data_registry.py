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

from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any

from core.market_data_contracts import ContractIdentity, ContractRoute


@dataclass(frozen=True)
class TmfSpreadQuote:
    """One synchronized, executable TMF near/far BBO snapshot.

    This is a pure market-data value object.  It has no strategy, order, or
    broker side effects.  Executable prices deliberately use BBO, never last.
    """

    near_code: str | None
    far_code: str | None
    near_bid: float | None
    near_ask: float | None
    far_bid: float | None
    far_ask: float | None
    spread_bid: float | None
    spread_ask: float | None
    spread_mid: float | None
    spread_width: float | None
    tick_size: float
    price_unit: str
    near_quote_time: Any
    far_quote_time: Any
    near_quote_age_ms: float | None
    far_quote_age_ms: float | None
    leg_time_skew_ms: float | None
    data_quality: str
    rejection_reason: str | None


def _quote_time_ms(value: Any) -> float | None:
    """Parse an explicit quote timestamp without fabricating one."""
    try:
        if isinstance(value, datetime):
            return value.timestamp() * 1000.0
        if value is None:
            return None
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text).timestamp() * 1000.0
    except (TypeError, ValueError, OverflowError):
        return None


def build_tmf_spread_quote(
    near_quote: dict[str, Any] | None,
    far_quote: dict[str, Any] | None,
    *,
    now: datetime,
    max_quote_age_ms: int,
    max_leg_time_skew_ms: int,
    tick_size: float = 1.0,
    price_unit: str = "index_points",
    session_confirmed: bool = False,
    trading_date_confirmed: bool = False,
) -> TmfSpreadQuote:
    """Build a fail-closed TMF executable spread quote.

    ``spread_bid = near_bid - far_ask`` (Sell Near / Buy Far) and
    ``spread_ask = near_ask - far_bid`` (Buy Near / Sell Far).  Both legs
    must be TMF contracts, have valid non-crossed BBOs, and be fresh and
    synchronized.  A stale far quote is never carried forward.
    """
    near_code = near_quote.get("code") if near_quote else None
    far_code = far_quote.get("code") if far_quote else None

    def blocked(reason: str, near_age=None, far_age=None, skew=None):
        return TmfSpreadQuote(
            near_code, far_code,
            _number(near_quote, "bid"), _number(near_quote, "ask"),
            _number(far_quote, "bid"), _number(far_quote, "ask"),
            None, None, None,
            None, tick_size, price_unit,
            near_quote.get("quote_time") if near_quote else None,
            far_quote.get("quote_time") if far_quote else None,
            near_age, far_age, skew, "BLOCKED", reason,
        )

    if near_quote is None:
        return blocked("MISSING_NEAR_QUOTE")
    if far_quote is None:
        return blocked("MISSING_FAR_QUOTE")
    if not _is_tmf_code(near_code) or not _is_tmf_code(far_code):
        return blocked("CONTRACT_ALIAS_MISMATCH")
    if not session_confirmed:
        return blocked("SESSION_UNCONFIRMED")
    if not trading_date_confirmed:
        return blocked("TRADING_DATE_UNCONFIRMED")

    now_ms = _quote_time_ms(now)
    near_ms = _quote_time_ms(near_quote.get("quote_time"))
    far_ms = _quote_time_ms(far_quote.get("quote_time"))
    if now_ms is None or near_ms is None or far_ms is None:
        return blocked("INVALID_QUOTE_TIMESTAMP")
    near_age = now_ms - near_ms
    far_age = now_ms - far_ms
    skew = abs(near_ms - far_ms)
    if near_age < 0 or far_age < 0:
        return blocked("INVALID_QUOTE_TIMESTAMP", near_age, far_age, skew)
    if near_age > max_quote_age_ms or far_age > max_quote_age_ms:
        return blocked("STALE_QUOTE", near_age, far_age, skew)
    if skew > max_leg_time_skew_ms:
        return blocked("QUOTE_SKEW_TOO_LARGE", near_age, far_age, skew)

    nb, na = _number(near_quote, "bid"), _number(near_quote, "ask")
    fb, fa = _number(far_quote, "bid"), _number(far_quote, "ask")
    if None in (nb, na, fb, fa):
        return blocked("MISSING_BIDASK", near_age, far_age, skew)
    if nb <= 0 or na <= 0 or fb <= 0 or fa <= 0:
        return blocked("INVALID_BIDASK", near_age, far_age, skew)
    if na < nb or fa < fb:
        return blocked("CROSSED_MARKET", near_age, far_age, skew)

    spread_bid = nb - fa
    spread_ask = na - fb
    return TmfSpreadQuote(
        near_code, far_code, nb, na, fb, fa, spread_bid, spread_ask,
        (spread_bid + spread_ask) / 2.0,
        spread_ask - spread_bid,
        tick_size, price_unit,
        near_quote.get("quote_time"), far_quote.get("quote_time"),
        near_age, far_age, skew, "VALID", None,
    )


def _number(quote: dict[str, Any] | None, key: str) -> float | None:
    if not quote or quote.get(key) is None:
        return None
    try:
        return float(quote[key])
    except (TypeError, ValueError):
        return None


def _is_tmf_code(code: Any) -> bool:
    if not isinstance(code, str) or not code.startswith("TMF"):
        return False
    return len(code) >= 5 and code[3].isalpha() and code[4:].isdigit()


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
