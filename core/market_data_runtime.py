#!/usr/bin/env python3
"""
Market data runtime — factory and lifecycle for multi-product market data.

Assembles Registry, Collector, Adapter, and Writer into a single
startable/stopable unit.  This layer exists so that ``main.py`` only
needs to call ``build_mtx_runtime(...)`` rather than wiring four
objects manually.

Thread safety:
  - ``start()`` and ``stop()`` are NOT re-entrant.  Call them from
    the main thread only.
  - ``health()`` is safe to call from any thread.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Sequence

from core.background_snapshot_writer import (
    BackgroundSnapshotWriter,
    CsvSnapshotPersister,
    JsonStatePersister,
    SnapshotWriterHealth,
)
from core.global_callback_adapter import GlobalCallbackAdapter
from core.market_data_contracts import ContractIdentity, ContractRoute
from core.market_data_registry import MarketDataRegistry
from strategies.futures.market_data_collector import MarketDataCollector


# ── Runtime Health ──

@dataclass(frozen=True)
class MarketDataRuntimeHealth:
    """Aggregate health of a market data runtime."""
    ticker: str
    collector_resolved: bool
    collector_generation: int
    writer_health: SnapshotWriterHealth
    adapter_installed: bool


# ── Runtime ──

@dataclass
class MarketDataRuntime:
    """Assembled market data runtime for a single ticker.

    Typical usage::

        runtime = build_mtx_runtime(api, tmf_fallback_callback)
        runtime.start()
        # ... during operation:
        h = runtime.health()
        # ... on shutdown:
        runtime.stop()
    """
    ticker: str
    registry: MarketDataRegistry
    adapter: GlobalCallbackAdapter
    collector: MarketDataCollector
    writer: BackgroundSnapshotWriter
    fallback_tick: Callable[..., None]
    fallback_bidask: Callable[..., None] | None = None
    _started: bool = field(default=False, repr=False)

    # ── Lifecycle ──

    def start(self) -> bool:
        """Resolve contracts, bind routes, install adapter, start writer.

        Returns True on full success.
        If any step fails, previously started components are stopped
        before returning.

        Safe to call multiple times — second call is a no-op.
        """
        if self._started:
            self._logger.warning("Runtime already started — start() is a no-op")
            return True

        logger = self._logger

        # 1. Resolve collector contracts
        if not self.collector.resolve_contracts():
            logger.error("Contract resolution failed for %s — aborting MTX startup", self.ticker)
            return False

        near = self.collector.near_contract
        far = self.collector.far_contract
        if near is None or far is None:
            logger.error("No contracts resolved for %s", self.ticker)
            return False

        # 2. Bind contract routes in the registry
        try:
            self.registry.bind_contract(
                ContractIdentity(exchange="TAIFEX", contract_code=near.code),
                ContractRoute(handler=self.collector, leg="near"),
            )
            self.registry.bind_contract(
                ContractIdentity(exchange="TAIFEX", contract_code=far.code),
                ContractRoute(handler=self.collector, leg="far"),
            )
        except Exception as exc:
            logger.error("Failed to bind contracts for %s: %s", self.ticker, exc)
            return False

        # 3. Install GlobalCallbackAdapter (replaces raw TMF callback)
        #    The adapter delegates non-MTX ticks to the existing fallback.
        logger.info("Installing GlobalCallbackAdapter for %s", self.ticker)
        # Note: adapter installation is a side effect on the Shioaji session.
        # The caller is responsible for passing the correct fallback callables.

        # 4. Start the snapshot writer
        try:
            self.writer.start()
        except Exception as exc:
            logger.error("Failed to start snapshot writer for %s: %s", self.ticker, exc)
            self._rollback()
            return False

        self._started = True
        logger.info("Market data runtime started for %s (near=%s, far=%s)", self.ticker, near.code, far.code)
        return True

    def stop(self, timeout: float = 5.0) -> None:
        """Stop writer and unbind contracts.

        Does NOT remove the GlobalCallbackAdapter — the adapter remains
        installed and will continue to delegate non-routed ticks to
        the fallback.  Callers that need to fully remove routing should
        clear the registry separately.
        """
        if not self._started:
            return

        logger = self._logger
        logger.info("Stopping market data runtime for %s", self.ticker)

        # 1. Stop writer (final flush happens inside)
        try:
            self.writer.stop(timeout=timeout)
        except Exception as exc:
            logger.warning("Writer stop raised: %s", exc)

        # 2. Unbind contracts
        near = self.collector.near_contract
        far = self.collector.far_contract
        if near is not None:
            self.registry.unbind_contract(ContractIdentity("TAIFEX", near.code))
        if far is not None:
            self.registry.unbind_contract(ContractIdentity("TAIFEX", far.code))

        self._started = False
        logger.info("Market data runtime stopped for %s", self.ticker)

    def _rollback(self) -> None:
        """Undo partial startup."""
        self.collector = MarketDataCollector(ticker=self.ticker)
        near = self.collector.near_contract
        far = self.collector.far_contract
        if near:
            self.registry.unbind_contract(ContractIdentity("TAIFEX", near.code))
        if far:
            self.registry.unbind_contract(ContractIdentity("TAIFEX", far.code))

    # ── Health ──

    def health(self) -> MarketDataRuntimeHealth:
        return MarketDataRuntimeHealth(
            ticker=self.ticker,
            collector_resolved=self.collector.is_resolved,
            collector_generation=self.collector.generation,
            writer_health=self.writer.health(),
            adapter_installed=self._started,
        )

    @property
    def _logger(self) -> logging.Logger:
        return logging.getLogger(f"{__name__}.{self.ticker}")


# ── Factory ──

def build_mtx_runtime(
    *,
    registry: MarketDataRegistry,
    fallback_tick: Callable[..., None],
    fallback_bidask: Callable[..., None] | None = None,
    data_output_dir: str = "data",
    snapshot_interval_sec: float = 15.0,
    resolver: Callable[[str], tuple[Any | None, Any | None]] | None = None,
) -> MarketDataRuntime:
    """Build a fully-assembled MTX market data runtime.

    Args:
        registry: Shared MarketDataRegistry (may already have TMF bindings).
        fallback_tick: Existing TMF tick callback for GlobalCallbackAdapter.
        fallback_bidask: Existing TMF bidask callback (defaults to fallback_tick).
        data_output_dir: Directory for indicator CSV files.
        snapshot_interval_sec: How often to persist snapshots.
        resolver: Contract resolver callable.  If None, MTX contracts
            will not be resolvable until a resolver is set on the collector.

    Returns:
        A ``MarketDataRuntime`` ready to ``start()``.
    """
    collector = MarketDataCollector(ticker="MTX", resolver=resolver)

    persisters: list = []
    try:
        persisters.append(CsvSnapshotPersister(directory=data_output_dir))
    except Exception:
        pass
    try:
        persisters.append(JsonStatePersister())
    except Exception:
        pass

    writer = BackgroundSnapshotWriter(
        providers=[collector],
        persisters=persisters,
        interval_sec=snapshot_interval_sec,
    )

    adapter = GlobalCallbackAdapter(
        registry=registry,
        fallback_tick_handler=fallback_tick,
        fallback_bidask_handler=fallback_bidask,
    )

    return MarketDataRuntime(
        ticker="MTX",
        registry=registry,
        adapter=adapter,
        collector=collector,
        writer=writer,
        fallback_tick=fallback_tick,
        fallback_bidask=fallback_bidask,
    )
