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
import math
import time
import threading
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from collections.abc import Callable
from typing import Any

from core.market_data_registry import MarketDataRegistry
from core.callback_trace_context import begin as _trace_begin, end as _trace_end, emit as _trace_emit


def _trace_scalar(value: object, converter):
    try:
        if value is None:
            return None
        if hasattr(value, "__getitem__") and not isinstance(value, (str, bytes)):
            value = value[0]
        value = converter(value)
        return value if converter is not float or math.isfinite(value) else None
    except (TypeError, ValueError, IndexError):
        return None


def _trace_fields(obj: object) -> dict[str, Any]:
    """Extract callback evidence; never synthesize BBO from close/last."""
    raw_time = str(getattr(obj, "datetime", "") or "")
    def first_present(*names):
        for name in names:
            value = getattr(obj, name, None)
            if value is not None:
                return value
        return None
    normalized = ""
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            normalized = datetime.strptime(raw_time, fmt).replace(
                tzinfo=ZoneInfo("Asia/Taipei")).astimezone(timezone.utc).isoformat()
            break
        except ValueError:
            continue
    return {
        "exchange_event_time_raw": raw_time,
        "exchange_event_time_normalized_utc": normalized,
        "bid1": _trace_scalar(first_present("bid_price", "buy_price"), float),
        "ask1": _trace_scalar(first_present("ask_price", "sell_price"), float),
        "bid_qty1": _trace_scalar(first_present("bid_volume", "buy_volume"), int),
        "ask_qty1": _trace_scalar(first_present("ask_volume", "sell_volume"), int),
        "last": _trace_scalar(getattr(obj, "close", None), float),
        "volume": _trace_scalar(getattr(obj, "volume", None), int),
    }


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
        capture_hook: Any = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._registry = registry
        self._fallback_tick = fallback_tick_handler
        self._fallback_bidask = fallback_bidask_handler or fallback_tick_handler
        self._always_call_fallback = always_call_fallback
        self._capture_hook = capture_hook  # DTI-001A: instrumentation only, None = disabled
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._callback_error_count: int = 0
        self._trace_seq: int = 0
        self._trace_lock = threading.Lock()

    @property
    def callback_error_count(self) -> int:
        """Cumulative count of caught routed-handler exceptions."""
        return self._callback_error_count

    def _capture_callback_trace(self, **fields: Any) -> None:
        """Send bounded callback evidence without affecting dispatch."""
        hook = self._capture_hook
        if hook is None:
            return
        try:
            queue = getattr(hook, "_queue", None)
            fields.setdefault("queue_depth", queue.qsize() if queue is not None else None)
            hook.observe_callback(**fields)
        except Exception:
            self._logger.exception("Callback trace failed (non-blocking)")

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
        _cb_wall = datetime.now(timezone.utc).isoformat()
        _cb_mono = time.monotonic_ns()
        with self._trace_lock:
            self._trace_seq += 1
            _cb_seq = self._trace_seq
        _trace_token = _trace_begin({
            "callback_id": f"{getattr(self._capture_hook, 'active_generation_id', '')}-cb{_cb_seq:08d}",
            "callback_seq": _cb_seq,
            "generation_id": getattr(self._capture_hook, "active_generation_id", ""),
            "callback_type": "TICK", "exchange": ex,
            "raw_contract_code": str(tick.code), "canonical_contract_code": code,
            "expected_role": getattr(_route, "leg", "unknown") if _route else "unknown",
            "callback_receive_wall_utc": _cb_wall,
            "callback_receive_monotonic_ns": _cb_mono,
            "hook": self._capture_hook,
            **_trace_fields(tick),
        })
        self._capture_callback_trace(callback_type="TICK", stage="ENTRY",
            exchange=ex, raw_contract_code=str(tick.code),
            canonical_contract_code=code,
            expected_role=getattr(_route, "leg", "unknown") if _route else "unknown",
            callback_receive_wall_utc=_cb_wall,
            callback_receive_monotonic_ns=_cb_mono,
            validation_result="RAW_ACCEPTED",
            drop_reason="UNROUTED_CONTRACT" if _route is None else "",
            **_trace_fields(tick))
        print(f"[GCA_TICK] BEFORE_DISPATCH adapter_id={id(self)} code={code} has_route={_has_route} "
              f"always_fallback={self._always_call_fallback} fallback_id={_disp_id}",
              flush=True)

        if _route is not None:
            _route_enter = time.monotonic_ns()
            try:
                _route.handler.on_tick(_route.leg, tick)
                _route_exit = time.monotonic_ns()
                self._capture_callback_trace(callback_type="TICK", stage="CONSUME",
                    exchange=ex, raw_contract_code=str(tick.code),
                    canonical_contract_code=code, expected_role=_route.leg,
                    callback_receive_wall_utc=_cb_wall,
                    callback_receive_monotonic_ns=_cb_mono,
                    route_enter_monotonic_ns=_route_enter,
                    route_exit_monotonic_ns=_route_exit,
                    strategy_consume_monotonic_ns=_route_exit,
                    validation_result="ROUTE_CONSUMED", **_trace_fields(tick))
                print(f"[GCA_TICK] ROUTE_OK adapter_id={id(self)} code={code} leg={_route.leg}", flush=True)
                try:
                    from core.market_data_health_registry import record_route, record_callback
                    record_callback(code, "tick")
                    record_route(code, _route.leg)
                except Exception:
                    pass
            except Exception:
                self._capture_callback_trace(callback_type="TICK", stage="ROUTE",
                    exchange=ex, raw_contract_code=str(tick.code),
                    canonical_contract_code=code, expected_role=_route.leg,
                    callback_receive_wall_utc=_cb_wall,
                    callback_receive_monotonic_ns=_cb_mono,
                    route_enter_monotonic_ns=_route_enter,
                    route_exit_monotonic_ns=time.monotonic_ns(),
                    validation_result="ROUTE_ERROR", drop_reason="HANDLER_EXCEPTION",
                    **_trace_fields(tick))
                self._callback_error_count += 1
                self._logger.exception(
                    "Routed handler failed for %s/%s (leg=%s); "
                    "fallback skipped to prevent double delivery",
                    exchange, tick.code, _route.leg,
                )
                print(f"[GCA_TICK] ROUTE_EXCEPTION adapter_id={id(self)} code={code}", flush=True)
            if not self._always_call_fallback:
                _trace_end(_trace_token)
                print(f"[GCA_TICK] EXIT routed_only adapter_id={id(self)} code={code}", flush=True)
                return

        if self._fallback_tick:
            try:
                _trace_emit("FALLBACK_CALLED", fallback_called=True,
                            validation_result="FALLBACK_DISPATCH")
                print(f"[GCA_TICK] FALLBACK_CALL adapter_id={id(self)} code={code}", flush=True)
                self._fallback_tick(exchange, tick)
                _trace_emit("FALLBACK_RETURNED", route_result="FALLBACK_RETURNED")
                print(f"[GCA_TICK] FALLBACK_OK adapter_id={id(self)} code={code}", flush=True)
                try:
                    from core.market_data_health_registry import record_callback
                    record_callback(code, "tick")
                except Exception:
                    pass
            except Exception:
                # [isolation] the tick fallback exception is contained
                # (counted + logged) — pre-existing callback fault
                # containment behavior, preserved.
                self._callback_error_count += 1
                self._logger.exception("Fallback handler failed for %s/%s", exchange, tick.code)
                print(f"[GCA_TICK] FALLBACK_EXCEPTION adapter_id={id(self)} code={code}", flush=True)
        else:
            print(f"[GCA_TICK] EXIT no_fallback adapter_id={id(self)} code={code}", flush=True)

        # ── DTI-001A: instrumentation-only tick capture ──
        if self._capture_hook is not None:
            try:
                self._capture_hook.observe(exchange, tick)
            except Exception:
                self._logger.exception("Capture hook observe failed (non-blocking)")
        _trace_end(_trace_token)

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
        _cb_wall = datetime.now(timezone.utc).isoformat()
        _cb_mono = time.monotonic_ns()
        with self._trace_lock:
            self._trace_seq += 1
            _cb_seq = self._trace_seq
        _trace_token = _trace_begin({
            "callback_id": f"{getattr(self._capture_hook, 'active_generation_id', '')}-cb{_cb_seq:08d}",
            "callback_seq": _cb_seq,
            "generation_id": getattr(self._capture_hook, "active_generation_id", ""),
            "callback_type": "BIDASK", "exchange": ex,
            "raw_contract_code": str(bidask.code), "canonical_contract_code": code,
            "expected_role": getattr(route, "leg", "unknown") if route else "unknown",
            "callback_receive_wall_utc": _cb_wall,
            "callback_receive_monotonic_ns": _cb_mono,
            "hook": self._capture_hook,
            **_trace_fields(bidask),
        })
        self._capture_callback_trace(callback_type="BIDASK", stage="ENTRY",
            exchange=ex, raw_contract_code=str(bidask.code),
            canonical_contract_code=code,
            expected_role=getattr(route, "leg", "unknown") if route else "unknown",
            callback_receive_wall_utc=_cb_wall,
            callback_receive_monotonic_ns=_cb_mono,
            validation_result="RAW_ACCEPTED",
            drop_reason="UNROUTED_CONTRACT" if route is None else "",
            **_trace_fields(bidask))

        if route is not None:
            _route_enter = time.monotonic_ns()
            try:
                # [fix] routed BidAsk goes to the handler's dedicated
                # on_bidask when available — never to on_tick (which is
                # for ticks); legacy TickHandler-only handlers keep the
                # on_tick(leg, bidask) routing for backward compatibility.
                _on_bidask = getattr(route.handler, "on_bidask", None)
                if callable(_on_bidask):
                    _on_bidask(route.leg, bidask)
                else:
                    route.handler.on_tick(route.leg, bidask)
                _route_exit = time.monotonic_ns()
                self._capture_callback_trace(callback_type="BIDASK", stage="CONSUME",
                    exchange=ex, raw_contract_code=str(bidask.code),
                    canonical_contract_code=code, expected_role=route.leg,
                    callback_receive_wall_utc=_cb_wall,
                    callback_receive_monotonic_ns=_cb_mono,
                    route_enter_monotonic_ns=_route_enter,
                    route_exit_monotonic_ns=_route_exit,
                    strategy_consume_monotonic_ns=_route_exit,
                    validation_result="ROUTE_CONSUMED", **_trace_fields(bidask))
            except Exception:
                self._capture_callback_trace(callback_type="BIDASK", stage="ROUTE",
                    exchange=ex, raw_contract_code=str(bidask.code),
                    canonical_contract_code=code, expected_role=route.leg,
                    callback_receive_wall_utc=_cb_wall,
                    callback_receive_monotonic_ns=_cb_mono,
                    route_enter_monotonic_ns=_route_enter,
                    route_exit_monotonic_ns=time.monotonic_ns(),
                    validation_result="ROUTE_ERROR", drop_reason="HANDLER_EXCEPTION",
                    **_trace_fields(bidask))
                self._callback_error_count += 1
                self._logger.exception(
                    "Routed bidask handler failed for %s/%s (leg=%s); "
                    "fallback skipped",
                    exchange, bidask.code, route.leg,
                )
            # 💡 Gemini CLI: If always_call_fallback is False, return early; otherwise also deliver to fallback handler
            if not self._always_call_fallback:
                _trace_end(_trace_token)
                return

        _trace_emit("FALLBACK_CALLED", fallback_called=True,
                    validation_result="FALLBACK_DISPATCH")
        try:
            self._fallback_bidask(exchange, bidask)
            _trace_emit("FALLBACK_RETURNED", route_result="FALLBACK_RETURNED")
        finally:
            # ContextVar cleanup is mandatory even when the legacy fallback
            # raises; callback correlation must never bleed into the next event.
            _trace_end(_trace_token)

        # ── DTI-001A: instrumentation-only bidask capture ──
        if self._capture_hook is not None:
            try:
                self._capture_hook.observe(exchange, bidask)
            except Exception:
                self._logger.exception("Capture hook observe failed (non-blocking, bidask)")
