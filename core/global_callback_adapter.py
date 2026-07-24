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
        always_call_fallback: bool = False,
        logger: logging.Logger | None = None,
    ) -> None:
        self._registry = registry
        self._fallback_tick = fallback_tick_handler
        self._fallback_bidask = fallback_bidask_handler or fallback_tick_handler
        self._always_call_fallback = always_call_fallback  # 💡 Gemini CLI: allow fallback tick_dispatcher to receive routed ticks
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._callback_error_count: int = 0

    @property
    def callback_error_count(self) -> int:
        """Cumulative count of caught routed-handler exceptions."""
        return self._callback_error_count

    # 💡 Gemini CLI: Accept *args to support both 1-arg (rshioaji 1.5+) and 2-arg (legacy) callback signatures
    def on_tick(self, *args) -> None:
        """Dispatch a single tick.

        1. Exact contract lookup → routed handler.
        2. Not found or always_call_fallback=True → fallback to existing TMF callback.
        """
        # ── P1: Three-stage diagnostic ──
        _tick_code = None
        if args:
            _last = args[-1]
            _tick_code = getattr(_last, "code", None) if hasattr(_last, "code") else None
        print(f"[GCA_TICK] ENTER adapter_id={id(self)} args={len(args)} code={_tick_code}", flush=True)

        if len(args) == 1:
            tick = args[0]
            exchange = getattr(tick, "exchange", "TFE")
        elif len(args) >= 2:
            exchange, tick = args[0], args[1]
        else:
            return

        if tick is None or not hasattr(tick, "code"):
            print(f"[GCA_TICK] EXIT no_code adapter_id={id(self)}", flush=True)
            return

        ex = normalize_exchange(exchange)
        code = normalize_contract_code(tick.code)

        _disp_id = id(self._fallback_tick) if self._fallback_tick else 0
        _route = self._registry.lookup(ex, code)
        _has_route = _route is not None
        print(f"[GCA_TICK] BEFORE_DISPATCH adapter_id={id(self)} code={code} has_route={_has_route} "
              f"always_fallback={self._always_call_fallback} fallback_id={_disp_id}",
              flush=True)

        if _route is not None:
            try:
                _route.handler.on_tick(_route.leg, tick)
                print(f"[GCA_TICK] ROUTE_OK adapter_id={id(self)} code={code} leg={_route.leg}", flush=True)
            except Exception:
                self._callback_error_count += 1
                self._logger.exception(
                    "Routed handler failed for %s/%s (leg=%s); "
                    "fallback skipped to prevent double delivery",
                    exchange, tick.code, _route.leg,
                )
                print(f"[GCA_TICK] ROUTE_EXCEPTION adapter_id={id(self)} code={code}", flush=True)
            if not self._always_call_fallback:
                print(f"[GCA_TICK] EXIT routed_only adapter_id={id(self)} code={code}", flush=True)
                return

        if self._fallback_tick:
            try:
                print(f"[GCA_TICK] FALLBACK_CALL adapter_id={id(self)} code={code}", flush=True)
                self._fallback_tick(*args)
                print(f"[GCA_TICK] FALLBACK_OK adapter_id={id(self)} code={code}", flush=True)
            except Exception:
                self._callback_error_count += 1
                self._logger.exception("Fallback handler failed for %s/%s", exchange, tick.code)
                print(f"[GCA_TICK] FALLBACK_EXCEPTION adapter_id={id(self)} code={code}", flush=True)
        else:
            print(f"[GCA_TICK] EXIT no_fallback adapter_id={id(self)} code={code}", flush=True)

    # 💡 Gemini CLI: Accept *args to support both 1-arg (rshioaji 1.5+) and 2-arg (legacy) callback signatures
    def on_bidask(self, *args) -> None:
        """Dispatch a single bidask update.  Same delegation logic as on_tick."""
        if len(args) == 1:
            bidask = args[0]
            exchange = getattr(bidask, "exchange", "TFE")
        elif len(args) >= 2:
            exchange, bidask = args[0], args[1]
        else:
            return

        if bidask is None or not hasattr(bidask, "code"):
            return

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
            # 💡 Gemini CLI: If always_call_fallback is False, return early; otherwise also deliver to fallback handler
            if not self._always_call_fallback:
                return

        self._fallback_bidask(*args)
