#!/usr/bin/env python3
"""
Global callback adapter — single Shioaji tick/bidask callback owner.

Delegates ticks to registered market-data handlers via exact-contract
lookup, and falls back to the existing TMF callback for all other ticks.

Exception isolation: a routed handler failure never propagates to the
fallback, and never breaks the TMF callback path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.market_data_registry import MarketDataRegistry


def normalize_exchange(exchange: object) -> str:
    """Canonicalize an exchange identifier for registry lookup.

    Raises ValueError if exchange is None or empty after normalization.
    """
    if exchange is None:
        raise ValueError("exchange must not be None or empty")
    value = getattr(exchange, "value", exchange)
    normalized = str(value).strip().upper()
    if not normalized:
        raise ValueError("exchange must not be None or empty")
    return normalized


def normalize_contract_code(code: object) -> str:
    """Canonicalize a contract code for registry lookup.

    Raises ValueError if code is None or empty after normalization.
    """
    if code is None:
        raise ValueError("contract code must not be None or empty")
    normalized = str(code).strip().upper()
    if not normalized:
        raise ValueError("contract code must not be None or empty")
    return normalized


class GlobalCallbackAdapter:
    """Adapts Shioaji tick/bidask callbacks into routed delivery.

    Usage::

        adapter = GlobalCallbackAdapter(
            registry=registry,
            fallback_handler=existing_tmf_tick_callback,
        )
        api.set_on_tick_callback(adapter.on_tick)
        api.set_on_bidask_callback(adapter.on_bidask)

    The adapter owns the callback slot — no other code registers a
    second callback on the same Shioaji session.
    """

    def __init__(
        self,
        registry: MarketDataRegistry,
        fallback_tick_handler: Callable[..., None],
        fallback_bidask_handler: Callable[..., None] | None = None,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._registry = registry
        self._fallback_tick = fallback_tick_handler
        self._fallback_bidask = fallback_bidask_handler or fallback_tick_handler
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._callback_error_count: int = 0

    @property
    def callback_error_count(self) -> int:
        """Cumulative count of caught routed-handler exceptions."""
        return self._callback_error_count

    def on_tick(self, exchange: object, tick: Any) -> None:
        """Dispatch a single tick.

        1. Exact contract lookup → routed handler.
        2. Not found → fallback to existing TMF callback.

        Routed handler exceptions are caught and logged; the fallback
        path is NOT attempted when routing succeeds, even on failure.
        """
        ex = normalize_exchange(exchange)
        code = normalize_contract_code(tick.code)
        route = self._registry.lookup(ex, code)

        if route is not None:
            try:
                route.handler.on_tick(route.leg, tick)
            except Exception:
                self._callback_error_count += 1
                self._logger.exception(
                    "Routed handler failed for %s/%s (leg=%s); "
                    "fallback skipped to prevent double delivery",
                    exchange, tick.code, route.leg,
                )
            return

        self._fallback_tick(exchange, tick)

    def on_bidask(self, exchange: object, bidask: Any) -> None:
        """Dispatch a single bidask update.  Same delegation logic as on_tick."""
        ex = normalize_exchange(exchange)
        code = normalize_contract_code(bidask.code)
        route = self._registry.lookup(ex, code)

        if route is not None:
            try:
                route.handler.on_tick(route.leg, bidask)
            except Exception:
                self._callback_error_count += 1
                self._logger.exception(
                    "Routed bidask handler failed for %s/%s (leg=%s); "
                    "fallback skipped",
                    exchange, bidask.code, route.leg,
                )
            return

        self._fallback_bidask(exchange, bidask)
