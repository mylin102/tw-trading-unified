#!/usr/bin/env python3
"""Tests for core.market_data_contracts and core.market_data_registry."""

from typing import Any
import pytest

from core.market_data_contracts import (
    ContractIdentity,
    ContractRoute,
    TickHandler,
)
from core.market_data_registry import (
    DuplicateContractBindingError,
    MarketDataRegistry,
)


# ── Helpers ──

class _SpyHandler:
    """Records ticks received for test assertions."""

    def __init__(self) -> None:
        self.received: list[tuple[str, Any]] = []

    def on_tick(self, leg: str, tick: Any) -> None:
        self.received.append((leg, tick))


# ── ContractIdentity ──

class TestContractIdentity:
    def test_equality(self) -> None:
        a = ContractIdentity(exchange="TAIFEX", contract_code="TMFH6")
        b = ContractIdentity(exchange="TAIFEX", contract_code="TMFH6")
        assert a == b

    def test_hashable(self) -> None:
        s = {ContractIdentity("TAIFEX", "TMFH6")}
        assert ContractIdentity("TAIFEX", "TMFH6") in s
        assert ContractIdentity("TAIFEX", "MTXI6") not in s

    def test_immutable(self) -> None:
        ident = ContractIdentity("TAIFEX", "TMFH6")
        with pytest.raises(AttributeError):
            ident.exchange = "OTHER"  # type: ignore[misc]


# ── ContractRoute ──

class TestContractRoute:
    def test_holds_handler_and_leg(self) -> None:
        handler = _SpyHandler()
        route = ContractRoute(handler=handler, leg="near")
        assert route.handler is handler
        assert route.leg == "near"


# ── TickHandler Protocol ──

class TestTickHandlerProtocol:
    def test_spy_handler_conforms(self) -> None:
        assert isinstance(_SpyHandler(), TickHandler)

    def test_any_object_with_on_tick_conforms(self) -> None:
        class _Minimal:
            def on_tick(self, leg: str, tick: Any) -> None:
                pass
        assert isinstance(_Minimal(), TickHandler)


# ── MarketDataRegistry ──

class TestMarketDataRegistry:
    def test_bind_and_lookup_exact_contract(self) -> None:
        registry = MarketDataRegistry()
        handler = _SpyHandler()
        identity = ContractIdentity("TAIFEX", "TMFH6")
        route = ContractRoute(handler=handler, leg="near")

        registry.bind_contract(identity, route)

        assert registry.lookup("TAIFEX", "TMFH6") is route

    def test_unknown_contract_returns_none(self) -> None:
        registry = MarketDataRegistry()
        assert registry.lookup("TAIFEX", "UNKNOWN") is None

    def test_duplicate_binding_is_rejected(self) -> None:
        registry = MarketDataRegistry()
        handler = _SpyHandler()
        identity = ContractIdentity("TAIFEX", "TMFH6")

        registry.bind_contract(
            identity, ContractRoute(handler=handler, leg="near")
        )
        with pytest.raises(DuplicateContractBindingError):
            registry.bind_contract(
                identity, ContractRoute(handler=handler, leg="far")
            )

    def test_unbind_removes_route(self) -> None:
        registry = MarketDataRegistry()
        handler = _SpyHandler()
        identity = ContractIdentity("TAIFEX", "TMFH6")

        registry.bind_contract(
            identity, ContractRoute(handler=handler, leg="near")
        )
        registry.unbind_contract(identity)
        assert registry.lookup("TAIFEX", "TMFH6") is None

    def test_unbind_nonexistent_is_noop(self) -> None:
        registry = MarketDataRegistry()
        # Should not raise
        registry.unbind_contract(ContractIdentity("TAIFEX", "GHOST"))

    def test_clear_removes_all_routes(self) -> None:
        registry = MarketDataRegistry()
        handler = _SpyHandler()

        for code in ("TMFH6", "TMFI6", "MTXH6", "MTXI6"):
            registry.bind_contract(
                ContractIdentity("TAIFEX", code),
                ContractRoute(handler=handler, leg="near"),
            )

        assert registry.binding_count == 4
        registry.clear()
        assert registry.binding_count == 0

    def test_binding_count(self) -> None:
        registry = MarketDataRegistry()
        handler = _SpyHandler()

        assert registry.binding_count == 0
        registry.bind_contract(
            ContractIdentity("TAIFEX", "TMFH6"),
            ContractRoute(handler=handler, leg="near"),
        )
        assert registry.binding_count == 1

    def test_near_far_leg_is_preserved(self) -> None:
        registry = MarketDataRegistry()
        near_handler = _SpyHandler()
        far_handler = _SpyHandler()

        registry.bind_contract(
            ContractIdentity("TAIFEX", "TMFH6"),
            ContractRoute(handler=near_handler, leg="near"),
        )
        registry.bind_contract(
            ContractIdentity("TAIFEX", "TMFI6"),
            ContractRoute(handler=far_handler, leg="far"),
        )

        near_route = registry.lookup("TAIFEX", "TMFH6")
        far_route = registry.lookup("TAIFEX", "TMFI6")

        assert near_route is not None and near_route.leg == "near"
        assert far_route is not None and far_route.leg == "far"
