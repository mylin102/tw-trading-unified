#!/usr/bin/env python3
"""Tests for core.global_callback_adapter."""

from typing import Any
from enum import Enum
import pytest

from core.global_callback_adapter import (
    GlobalCallbackAdapter,
    normalize_exchange,
    normalize_contract_code,
)
from core.market_data_contracts import (
    ContractIdentity,
    ContractRoute,
    SpreadLeg,
    TickHandler,
)
from core.market_data_registry import MarketDataRegistry


# ── Test Doubles ──

class _RouteSpy:
    """Records routed ticks/bidasks for assertion."""

    def __init__(self) -> None:
        self.ticks: list[tuple[SpreadLeg, Any]] = []
        self.bidasks: list[tuple[SpreadLeg, Any]] = []

    def on_tick(self, leg: SpreadLeg, tick: Any) -> None:
        self.ticks.append((leg, tick))

    def on_bidask(self, leg: SpreadLeg, bidask: Any) -> None:
        self.bidasks.append((leg, bidask))


class _FallbackSpy:
    """Records fallback calls (exchange + tick/bidask)."""

    def __init__(self) -> None:
        self.ticks: list[tuple[Any, Any]] = []
        self.bidasks: list[tuple[Any, Any]] = []

    def on_tick(self, exchange: Any, tick: Any) -> None:
        self.ticks.append((exchange, tick))

    def on_bidask(self, exchange: Any, bidask: Any) -> None:
        self.bidasks.append((exchange, bidask))


class _FailingHandler:
    """Always raises on any call."""

    def on_tick(self, leg: SpreadLeg, tick: Any) -> None:
        raise RuntimeError(f"routed handler failed for leg={leg}")

    def on_bidask(self, leg: SpreadLeg, bidask: Any) -> None:
        raise RuntimeError(f"routed bidask handler failed for leg={leg}")


class _FailingFallback:
    """Always raises on tick or bidask."""

    def on_tick(self, exchange: Any, tick: Any) -> None:
        raise RuntimeError("TMF fallback tick failed")

    def on_bidask(self, exchange: Any, bidask: Any) -> None:
        raise RuntimeError("TMF fallback bidask failed")


class _MockTick:
    def __init__(self, code: str) -> None:
        self.code = code


class _MockBidAsk:
    def __init__(self, code: str) -> None:
        self.code = code


class _Exchange(Enum):
    TAIFEX = "taifex"


# ── SpreadLeg Type ──

class TestSpreadLeg:
    def test_route_spy_conforms_to_tick_handler_protocol(self) -> None:
        assert isinstance(_RouteSpy(), TickHandler)


# ── Normalization ──

class TestNormalization:
    def test_normalize_exchange_lower(self) -> None:
        assert normalize_exchange("taifex") == "TAIFEX"

    def test_normalize_exchange_upper(self) -> None:
        assert normalize_exchange("TAIFEX") == "TAIFEX"

    def test_normalize_exchange_strips_whitespace(self) -> None:
        assert normalize_exchange("  TAIFEX  ") == "TAIFEX"

    def test_normalize_exchange_with_enum(self) -> None:
        assert normalize_exchange(_Exchange.TAIFEX) == "TAIFEX"

    def test_normalize_exchange_rejects_none(self) -> None:
        with pytest.raises(ValueError, match="exchange must not be None or empty"):
            normalize_exchange(None)

    def test_normalize_exchange_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="exchange must not be None or empty"):
            normalize_exchange("")

    def test_normalize_exchange_rejects_whitespace(self) -> None:
        with pytest.raises(ValueError, match="exchange must not be None or empty"):
            normalize_exchange("   ")

    def test_normalize_contract_code_lower(self) -> None:
        assert normalize_contract_code("tmfh6") == "TMFH6"

    def test_normalize_contract_code_upper(self) -> None:
        assert normalize_contract_code("TMFH6") == "TMFH6"

    def test_normalize_contract_code_strips_whitespace(self) -> None:
        assert normalize_contract_code("  mtxh6  ") == "MTXH6"

    def test_normalize_contract_code_rejects_none(self) -> None:
        with pytest.raises(ValueError, match="contract code must not be None or empty"):
            normalize_contract_code(None)

    def test_normalize_contract_code_rejects_empty(self) -> None:
        with pytest.raises(ValueError, match="contract code must not be None or empty"):
            normalize_contract_code("")

    def test_normalize_contract_code_rejects_whitespace(self) -> None:
        with pytest.raises(ValueError, match="contract code must not be None or empty"):
            normalize_contract_code("   ")


# ── GlobalCallbackAdapter: Tick Routing ──

class TestTickRouting:
    def test_routed_tick_goes_only_to_registered_handler(self) -> None:
        registry = MarketDataRegistry()
        handler = _RouteSpy()
        registry.bind_contract(
            ContractIdentity("TAIFEX", "TMFH6"),
            ContractRoute(handler=handler, leg="near"),
        )

        fallback = _FallbackSpy()
        adapter = GlobalCallbackAdapter(registry, fallback.on_tick)
        tick = _MockTick("TMFH6")

        adapter.on_tick("TAIFEX", tick)

        assert handler.ticks == [("near", tick)]
        assert fallback.ticks == []
        assert handler.bidasks == []

    def test_routed_tick_preserves_leg(self) -> None:
        registry = MarketDataRegistry()
        near = _RouteSpy()
        far = _RouteSpy()

        registry.bind_contract(
            ContractIdentity("TAIFEX", "TMFH6"),
            ContractRoute(handler=near, leg="near"),
        )
        registry.bind_contract(
            ContractIdentity("TAIFEX", "TMFI6"),
            ContractRoute(handler=far, leg="far"),
        )

        adapter = GlobalCallbackAdapter(registry, _FallbackSpy().on_tick)

        near_tick = _MockTick("TMFH6")
        far_tick = _MockTick("TMFI6")
        adapter.on_tick("TAIFEX", near_tick)
        adapter.on_tick("TAIFEX", far_tick)

        assert near.ticks == [("near", near_tick)]
        assert far.ticks == [("far", far_tick)]

    def test_unrouted_tick_delegates_to_fallback(self) -> None:
        registry = MarketDataRegistry()
        fallback = _FallbackSpy()
        adapter = GlobalCallbackAdapter(registry, fallback.on_tick)

        tick = _MockTick("TMFH6")
        adapter.on_tick("TAIFEX", tick)

        assert fallback.ticks == [("TAIFEX", tick)]

    def test_adapter_normalizes_before_lookup(self) -> None:
        registry = MarketDataRegistry()
        handler = _RouteSpy()
        registry.bind_contract(
            ContractIdentity("TAIFEX", "MTXH6"),
            ContractRoute(handler=handler, leg="near"),
        )

        fallback = _FallbackSpy()
        adapter = GlobalCallbackAdapter(registry, fallback.on_tick)

        tick = _MockTick("  mtxh6 ")
        adapter.on_tick("taifex", tick)

        assert handler.ticks == [("near", tick)]
        assert fallback.ticks == []

    def test_routed_handler_exception_is_isolated(self) -> None:
        registry = MarketDataRegistry()
        registry.bind_contract(
            ContractIdentity("TAIFEX", "MTXH6"),
            ContractRoute(handler=_FailingHandler(), leg="near"),
        )

        fallback = _FallbackSpy()
        adapter = GlobalCallbackAdapter(registry, fallback.on_tick)

        # Must not raise — exception is caught and logged
        adapter.on_tick("TAIFEX", _MockTick("MTXH6"))
        assert fallback.ticks == []

    def test_routed_handler_exception_does_not_call_fallback(self) -> None:
        registry = MarketDataRegistry()
        registry.bind_contract(
            ContractIdentity("TAIFEX", "MTXH6"),
            ContractRoute(handler=_FailingHandler(), leg="near"),
        )
        registry.bind_contract(
            ContractIdentity("TAIFEX", "TMFH6"),
            ContractRoute(handler=_RouteSpy(), leg="near"),
        )

        fallback = _FallbackSpy()
        adapter = GlobalCallbackAdapter(registry, fallback.on_tick)

        adapter.on_tick("TAIFEX", _MockTick("MTXH6"))
        assert fallback.ticks == []

        tick = _MockTick("TMFH6")
        adapter.on_tick("TAIFEX", tick)
        assert fallback.ticks == []  # TMF is routed, not fallback

    # ── Fallback exception propagation ──

    def test_fallback_exception_propagates_on_tick(self) -> None:
        adapter = GlobalCallbackAdapter(
            MarketDataRegistry(),
            _FailingFallback().on_tick,
        )
        with pytest.raises(RuntimeError, match="TMF fallback tick failed"):
            adapter.on_tick("TAIFEX", _MockTick("TMFH6"))

    def test_fallback_exception_propagates_on_bidask(self) -> None:
        adapter = GlobalCallbackAdapter(
            MarketDataRegistry(),
            _FailingFallback().on_tick,
        )
        with pytest.raises(RuntimeError, match="TMF fallback tick failed"):
            adapter.on_bidask("TAIFEX", _MockBidAsk("TMFH6"))


# ── GlobalCallbackAdapter: BidAsk Routing ──

class TestBidAskRouting:
    def test_routed_bidask_goes_only_to_registered_handler(self) -> None:
        registry = MarketDataRegistry()
        handler = _RouteSpy()
        registry.bind_contract(
            ContractIdentity("TAIFEX", "TMFH6"),
            ContractRoute(handler=handler, leg="near"),
        )

        fallback = _FallbackSpy()
        adapter = GlobalCallbackAdapter(registry, fallback.on_tick, fallback.on_bidask)

        bidask = _MockBidAsk("TMFH6")
        adapter.on_bidask("TAIFEX", bidask)

        # Bidask routed via on_tick (TickHandler protocol)
        assert handler.ticks == [("near", bidask)]
        assert fallback.bidasks == []

    def test_unrouted_bidask_delegates_to_fallback(self) -> None:
        registry = MarketDataRegistry()
        fallback = _FallbackSpy()
        adapter = GlobalCallbackAdapter(registry, fallback.on_tick, fallback.on_bidask)

        bidask = _MockBidAsk("TMFH6")
        adapter.on_bidask("TAIFEX", bidask)

        assert fallback.bidasks == [("TAIFEX", bidask)]

    def test_routed_bidask_exception_is_isolated(self) -> None:
        registry = MarketDataRegistry()
        registry.bind_contract(
            ContractIdentity("TAIFEX", "MTXH6"),
            ContractRoute(handler=_FailingHandler(), leg="near"),
        )

        fallback = _FallbackSpy()
        adapter = GlobalCallbackAdapter(registry, fallback.on_tick, fallback.on_bidask)

        # Must not raise
        adapter.on_bidask("TAIFEX", _MockBidAsk("MTXH6"))
        assert fallback.bidasks == []
