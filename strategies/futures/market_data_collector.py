#!/usr/bin/env python3
"""
Passive market data collector.

A deterministic, thread-safe market-state cache — not a strategy, not a broker.
Receives ticks via ``on_tick()`` and produces immutable snapshots for
persistence downstream.

No OrderManager, no ExecutionContext, no strategy lifecycle, no submit capability.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol


# ── Errors ──

class ContractResolutionError(RuntimeError):
    """Raised when contract resolution fails."""


class UnknownLegError(ValueError):
    """Raised when an unrecognised leg label is supplied."""


# ── Snapshot Types ──

@dataclass(frozen=True)
class ContractInfo:
    """Read-only description of a resolved contract."""
    code: str
    product: str
    delivery_date: str
    expiry_date: datetime | None = None


@dataclass(frozen=True)
class MarketDataSnapshot:
    """Immutable point-in-time snapshot of collector state.

    .. note::
       All prices are ``None`` until the first tick arrives.
       ``None`` means "no data yet", never "zero".
    """
    ticker: str
    generation: int
    event_time: datetime | None
    captured_at: datetime

    near_contract: ContractInfo | None
    far_contract: ContractInfo | None

    near_last: float | None
    far_last: float | None

    @property
    def has_data(self) -> bool:
        return self.near_last is not None and self.far_last is not None

    @property
    def spread(self) -> float | None:
        if self.near_last is not None and self.far_last is not None:
            return self.near_last - self.far_last
        return None


# ── Resolver Protocol ──

class ContractResolverFn(Protocol):
    """Protocol for an external contract resolver callable.

    Accepts a ticker string and returns a tuple of (near_contract, far_contract).
    Each contract must have at least ``code`` and ``delivery_date`` attributes.
    Returns (None, None) when resolution fails.
    """

    def __call__(self, ticker: str) -> tuple[Any | None, Any | None]:
        ...


# ── Collector ──

_INTERNAL_LEGS = frozenset({"near", "far"})


class MarketDataCollector:
    """Passive market-data state cache.

    Thread-safe: ``on_tick`` and ``snapshot_for_persistence`` share a single lock.
    Lock is held only for scalar/state updates — no I/O, no heavy computation.

    Usage::

        collector = MarketDataCollector(ticker="MTX", resolver=my_resolver)
        collector.resolve_contracts()
        # ... later, from callback thread:
        collector.on_tick("near", tick)
        # ... from writer thread:
        snap = collector.snapshot_for_persistence()
    """

    def __init__(
        self,
        ticker: str,
        *,
        resolver: ContractResolverFn | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._ticker = ticker.upper()
        self._resolver = resolver
        self._logger = logger or logging.getLogger(f"{__name__}.{ticker}")

        # State — all protected by _lock
        self._lock = threading.Lock()
        self._near_contract: ContractInfo | None = None
        self._far_contract: ContractInfo | None = None
        self._near_last: float | None = None
        self._far_last: float | None = None
        self._event_time: datetime | None = None
        self._generation: int = 0
        self._resolved: bool = False

    # ── Properties ──

    @property
    def ticker(self) -> str:
        return self._ticker

    @property
    def is_resolved(self) -> bool:
        return self._resolved

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def near_contract(self) -> ContractInfo | None:
        with self._lock:
            return self._near_contract

    @property
    def far_contract(self) -> ContractInfo | None:
        with self._lock:
            return self._far_contract

    @property
    def near_last(self) -> float | None:
        with self._lock:
            return self._near_last

    @property
    def far_last(self) -> float | None:
        with self._lock:
            return self._far_last

    # ── Resolution ──

    def resolve_contracts(self) -> bool:
        """Resolve near/far contracts via the configured resolver.

        Returns True when both contracts are resolved.
        Safe to call multiple times — re-resolution is a no-op after success.
        """
        if self._resolved:
            return True

        if self._resolver is None:
            self._logger.warning("No resolver configured — cannot resolve contracts")
            return False

        try:
            near_raw, far_raw = self._resolver(self._ticker)
        except Exception as exc:
            raise ContractResolutionError(
                f"Resolver raised for {self._ticker}: {exc}"
            ) from exc

        if near_raw is None or far_raw is None:
            return False

        with self._lock:
            self._near_contract = self._to_contract_info(near_raw)
            self._far_contract = self._to_contract_info(far_raw)
            self._resolved = True

        return True

    @staticmethod
    def _to_contract_info(raw: Any) -> ContractInfo:
        delivery = getattr(raw, "delivery_date", "") or ""
        return ContractInfo(
            code=str(getattr(raw, "code", "")),
            product=str(getattr(raw, "product", "") or getattr(raw, "code", "")[:3]),
            delivery_date=delivery,
        )

    # ── Tick Ingestion ──

    def on_tick(self, leg: str, tick: Any) -> None:
        """Process a single tick for a given leg.

        ``leg`` must be ``"near"`` or ``"far"``.
        Updates the corresponding last price, increments generation,
        and records event time.

        This is the only method intended to be called from the Shioaji
        callback thread.  No I/O, no heavy computation.
        """
        if leg not in _INTERNAL_LEGS:
            raise UnknownLegError(f"Unknown leg: {leg!r}; expected one of {_INTERNAL_LEGS}")

        price = self._extract_price(tick)
        if price is None:
            return

        with self._lock:
            self._generation += 1
            self._event_time = datetime.now()
            if leg == "near":
                self._near_last = price
            else:
                self._far_last = price

    @staticmethod
    def _extract_price(tick: Any) -> float | None:
        """Extract a numeric price from a tick/bidask object."""
        for attr in ("close", "Close", "price", "Price", "bid", "ask", "last"):
            val = getattr(tick, attr, None)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    continue
        return None

    # ── Snapshot ──

    def snapshot_for_persistence(self) -> MarketDataSnapshot:
        """Produce an immutable point-in-time snapshot.

        Returns a frozen dataclass — safe to pass across threads
        without additional locking.
        """
        with self._lock:
            return MarketDataSnapshot(
                ticker=self._ticker,
                generation=self._generation,
                event_time=self._event_time,
                captured_at=datetime.now(),
                near_contract=self._near_contract,
                far_contract=self._far_contract,
                near_last=self._near_last,
                far_last=self._far_last,
            )

    # ── Safety introspection (no execution capability) ──

    @property
    def has_no_order_capability(self) -> bool:
        """True — this collector cannot place orders.

        Useful for runtime assertions and tests.
        """
        return True

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"ticker={self._ticker!r}, "
            f"resolved={self._resolved}, "
            f"generation={self._generation}, "
            f"near_last={self._near_last}, "
            f"far_last={self._far_last})"
        )
