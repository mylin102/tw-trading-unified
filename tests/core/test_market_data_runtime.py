#!/usr/bin/env python3
"""Tests for core.market_data_runtime."""

from typing import Any
import pytest

from core.background_snapshot_writer import BackgroundSnapshotWriter, CsvSnapshotPersister, JsonStatePersister
from core.global_callback_adapter import GlobalCallbackAdapter
from core.market_data_contracts import ContractIdentity, ContractRoute
from core.market_data_registry import MarketDataRegistry
from core.market_data_runtime import MarketDataRuntime, MarketDataRuntimeHealth, build_mtx_runtime
from strategies.futures.market_data_collector import MarketDataCollector


# ── Helpers ──

class _MockContract:
    def __init__(self, code: str, delivery_date: str = "2026/09/16"):
        self.code = code
        self.delivery_date = delivery_date


def _mtx_resolver(ticker: str) -> tuple[Any, Any]:
    return (
        _MockContract(code="MTXH6"),
        _MockContract(code="MTXI6"),
    )


class _SpyFallback:
    def __init__(self):
        self.ticks = []
        self.bidasks = []

    def on_tick(self, exchange, tick):
        self.ticks.append((exchange, tick))

    def on_bidask(self, exchange, bidask):
        self.bidasks.append((exchange, bidask))


# ── Factory ──

class TestBuildMtxRuntime:
    def test_factory_returns_configured_runtime(self) -> None:
        registry = MarketDataRegistry()
        fallback = _SpyFallback()

        runtime = build_mtx_runtime(
            registry=registry,
            fallback_tick=fallback.on_tick,
            resolver=_mtx_resolver,
        )

        assert isinstance(runtime, MarketDataRuntime)
        assert runtime.ticker == "MTX"
        assert runtime.registry is registry
        assert isinstance(runtime.collector, MarketDataCollector)
        assert isinstance(runtime.writer, BackgroundSnapshotWriter)
        assert isinstance(runtime.adapter, GlobalCallbackAdapter)

    def test_factory_runtime_starts_successfully(self) -> None:
        registry = MarketDataRegistry()
        fallback = _SpyFallback()

        runtime = build_mtx_runtime(
            registry=registry,
            fallback_tick=fallback.on_tick,
            resolver=_mtx_resolver,
        )

        result = runtime.start()
        assert result is True

        h = runtime.health()
        assert h.collector_resolved is True
        assert h.ticker == "MTX"

        runtime.stop()

    def test_factory_initial_health(self) -> None:
        runtime = build_mtx_runtime(
            registry=MarketDataRegistry(),
            fallback_tick=lambda *a: None,
            resolver=_mtx_resolver,
        )

        h = runtime.health()
        assert isinstance(h, MarketDataRuntimeHealth)
        assert h.collector_resolved is False
        assert h.adapter_installed is False


# ── Runtime Lifecycle ──

class TestRuntimeLifecycle:
    def test_start_binds_contracts_in_registry(self) -> None:
        registry = MarketDataRegistry()
        runtime = build_mtx_runtime(
            registry=registry,
            fallback_tick=lambda *a: None,
            resolver=_mtx_resolver,
        )

        runtime.start()

        # Contracts should be bound
        near = registry.lookup("TAIFEX", "MTXH6")
        far = registry.lookup("TAIFEX", "MTXI6")
        assert near is not None
        assert near.leg == "near"
        assert far is not None
        assert far.leg == "far"

        runtime.stop()

    def test_stop_unbinds_contracts(self) -> None:
        registry = MarketDataRegistry()
        runtime = build_mtx_runtime(
            registry=registry,
            fallback_tick=lambda *a: None,
            resolver=_mtx_resolver,
        )

        runtime.start()
        runtime.stop()

        assert registry.lookup("TAIFEX", "MTXH6") is None
        assert registry.lookup("TAIFEX", "MTXI6") is None

    def test_start_is_idempotent(self) -> None:
        runtime = build_mtx_runtime(
            registry=MarketDataRegistry(),
            fallback_tick=lambda *a: None,
            resolver=_mtx_resolver,
        )

        assert runtime.start() is True
        assert runtime.start() is True  # second call is no-op
        runtime.stop()

    def test_stop_before_start_is_noop(self) -> None:
        runtime = build_mtx_runtime(
            registry=MarketDataRegistry(),
            fallback_tick=lambda *a: None,
            resolver=_mtx_resolver,
        )
        runtime.stop()  # should not raise

    def test_health_reflects_running_state(self) -> None:
        runtime = build_mtx_runtime(
            registry=MarketDataRegistry(),
            fallback_tick=lambda *a: None,
            resolver=_mtx_resolver,
        )

        assert runtime.health().adapter_installed is False

        runtime.start()
        assert runtime.health().adapter_installed is True

        runtime.stop()
        assert runtime.health().adapter_installed is False
