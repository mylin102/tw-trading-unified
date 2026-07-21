#!/usr/bin/env python3
"""
Market data infrastructure — core types and contracts.

These types define the boundary between Shioaji tick delivery and
market-data consumers.  No Shioaji dependency, no IO, no strategy imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, Protocol, TypeAlias, runtime_checkable


# ── Spread Leg ──

SpreadLeg: TypeAlias = Literal["near", "far"]


# ── Tick Handler Protocol ──

@runtime_checkable
class TickHandler(Protocol):
    """Protocol for any object that can consume a single tick.

    Designed as a Protocol so Registry / adapter tests can use
    plain mocks without importing collector or strategy classes.
    """

    def on_tick(self, leg: SpreadLeg, tick: Any) -> None:
        ...


# ── Contract Identity ──

@dataclass(frozen=True)
class ContractIdentity:
    """Canonical identity for a single futures/options contract.

    Used as the key in MarketDataRegistry lookups.
    Must be immutable and hashable (frozen dataclass).

    .. note::
       This class does NOT perform implicit canonicalization.
       All values must be canonicalized *before* binding or lookup
       by the caller (see ``GlobalCallbackAdapter.normalize_*``).
    """
    exchange: str           # e.g. "TAIFEX"
    contract_code: str      # e.g. "TMFH6", "MTXI6"


# ── Contract Route ──

@dataclass(frozen=True)
class ContractRoute:
    """Binding from a ContractIdentity to a tick handler.

    ``handler`` receives ticks for this contract.
    ``leg`` distinguishes near-month from far-month within a calendar spread.
    """
    handler: TickHandler
    leg: SpreadLeg
