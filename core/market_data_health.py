#!/usr/bin/env python3
"""
Market data runtime health model.

A deterministic, IO-free health evaluator that produces an immutable
snapshot of the market data runtime's operational status.

Designed for:
  - soak-test evidence collection
  - PM2 / watchdog integration
  - incident diagnosis
  - dashboard read-only display
  - pre/post restart state comparison
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ── Health Reason Codes ──

class HealthReason(str, Enum):
    """Deterministic reason codes for degraded/unhealthy status.

    Each code has a fixed string value so that downstream consumers
    (dashboard, watchdog, soak analyzer) never depend on free-text parsing.
    """
    REGISTRY_BINDING_INCOMPLETE = "REGISTRY_BINDING_INCOMPLETE"
    CONTRACT_BINDING_INCOMPLETE = "CONTRACT_BINDING_INCOMPLETE"
    NEAR_TICK_MISSING = "NEAR_TICK_MISSING"
    FAR_TICK_MISSING = "FAR_TICK_MISSING"
    NEAR_TICK_STALE = "NEAR_TICK_STALE"
    FAR_TICK_STALE = "FAR_TICK_STALE"
    WRITER_NOT_RUNNING = "WRITER_NOT_RUNNING"
    WRITER_FAILURES = "WRITER_FAILURES"
    CALLBACK_ERRORS = "CALLBACK_ERRORS"
    NO_TICKS_RECEIVED = "NO_TICKS_RECEIVED"
    WRITER_NEVER_SUCCEEDED = "WRITER_NEVER_SUCCEEDED"


# ── Status ──

class RuntimeHealthStatus(str, Enum):
    """Aggregate runtime health level.

    .. py:attribute:: STOPPED
       Runtime not started or already stopped.

    .. py:attribute:: UNHEALTHY
       A critical invariant is broken (writer thread died,
       callback path corrupted, snapshot acquisition failed).

    .. py:attribute:: DEGRADED
       Runtime running but with observable issues:
       stale ticks, writer failures, incomplete data.

    .. py:attribute:: HEALTHY
       All components operating within configured thresholds.
    """
    STOPPED = "STOPPED"
    UNHEALTHY = "UNHEALTHY"
    DEGRADED = "DEGRADED"
    HEALTHY = "HEALTHY"


# ── Health Snapshot ──

@dataclass(frozen=True)
class MarketDataRuntimeHealth:
    """Immutable point-in-time health snapshot.

    Thread-safe by construction — all fields are immutable.
    Use ``to_dict()`` for serialisation.
    """

    # ── Identity ──
    ticker: str
    status: RuntimeHealthStatus
    observed_at: datetime

    # ── Runtime lifecycle ──
    runtime_started: bool
    registry_binding_count: int

    # ── Contracts ──
    near_contract_code: str | None = None
    far_contract_code: str | None = None

    # ── Collector state ──
    collector_generation: int = 0
    near_tick_age_ms: int | None = None
    far_tick_age_ms: int | None = None

    # ── Writer state ──
    writer_running: bool = False
    writer_last_success_at: datetime | None = None
    writer_consecutive_failures: int = 0

    # ── Adapter / error tracking ──
    callback_error_count: int = 0

    # ── Degradation details ──
    degraded_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict.

        No ``datetime`` objects — all timestamps are ISO-format strings.
        """
        return {
            "ticker": self.ticker,
            "status": self.status.value,
            "observed_at": self.observed_at.isoformat(),
            "runtime_started": self.runtime_started,
            "registry_binding_count": self.registry_binding_count,
            "near_contract_code": self.near_contract_code,
            "far_contract_code": self.far_contract_code,
            "collector_generation": self.collector_generation,
            "near_tick_age_ms": self.near_tick_age_ms,
            "far_tick_age_ms": self.far_tick_age_ms,
            "writer_running": self.writer_running,
            "writer_last_success_at": (
                self.writer_last_success_at.isoformat()
                if self.writer_last_success_at is not None else None
            ),
            "writer_consecutive_failures": self.writer_consecutive_failures,
            "callback_error_count": self.callback_error_count,
            "degraded_reasons": list(self.degraded_reasons),
        }


# ── Health Evaluator ──

class MarketDataHealthEvaluator:
    """Deterministic, IO-free health evaluator.

    Takes raw runtime state snapshots and produces a structured
    ``MarketDataRuntimeHealth`` with an aggregate status.

    Thresholds and session-awareness are injected at construction
    time, making the evaluator testable without a running system.
    """

    def __init__(
        self,
        ticker: str,
        *,
        tick_stale_after_ms: int = 5_000,
    ) -> None:
        self._ticker = ticker
        self._tick_stale_after_ms = tick_stale_after_ms

    def evaluate(
        self,
        *,
        runtime_started: bool,
        registry_binding_count: int,
        near_contract_code: str | None,
        far_contract_code: str | None,
        collector_generation: int,
        near_last_updated_at: float | None,
        far_last_updated_at: float | None,
        writer_running: bool,
        writer_last_success_at: float | None,
        writer_consecutive_failures: int,
        callback_error_count: int,
        market_expected_open: bool,
    ) -> MarketDataRuntimeHealth:
        """Produce a health snapshot from raw runtime state.

        Args:
            runtime_started: Whether the runtime was started.
            registry_binding_count: Number of bound contracts.
            near/far_contract_code: Resolved contract codes.
            collector_generation: Monotonic tick counter.
            near/far_last_updated_at: ``time.time()`` of last tick.
            writer_running: Writer thread alive flag.
            writer_last_success_at: ``time.time()`` of last successful write.
            writer_consecutive_failures: Consecutive writer cycle failures.
            callback_error_count: Cumulative callback errors.
            market_expected_open: True if ticks are expected
                (day or night session).  When False, stale ticks
                do NOT cause DEGRADED status.

        Returns:
            An immutable ``MarketDataRuntimeHealth``.
        """
        degraded: list[str] = []
        now = time.time()
        observed_at = datetime.now()

        # ── Status determination ──

        if not runtime_started:
            status = RuntimeHealthStatus.STOPPED
        elif not writer_running:
            status = RuntimeHealthStatus.UNHEALTHY
            degraded.append(HealthReason.WRITER_NOT_RUNNING.value)
        else:
            # Evaluate degradation factors
            self._assess_tick_staleness(
                degraded, near_last_updated_at, far_last_updated_at,
                market_expected_open, now,
            )
            self._assess_writer_health(
                degraded, writer_consecutive_failures, writer_last_success_at,
            )
            self._assess_collector_state(degraded, collector_generation, near_last_updated_at)
            self._assess_callback_errors(degraded, callback_error_count)

            if not degraded:
                status = RuntimeHealthStatus.HEALTHY
            else:
                status = RuntimeHealthStatus.DEGRADED

        # ── Tick age computation ──

        near_age = self._tick_age_ms(near_last_updated_at, now)
        far_age = self._tick_age_ms(far_last_updated_at, now)

        return MarketDataRuntimeHealth(
            ticker=self._ticker,
            status=status,
            observed_at=observed_at,
            runtime_started=runtime_started,
            registry_binding_count=registry_binding_count,
            near_contract_code=near_contract_code,
            far_contract_code=far_contract_code,
            collector_generation=collector_generation,
            near_tick_age_ms=near_age,
            far_tick_age_ms=far_age,
            writer_running=writer_running,
            writer_last_success_at=(
                datetime.fromtimestamp(writer_last_success_at)
                if writer_last_success_at is not None else None
            ),
            writer_consecutive_failures=writer_consecutive_failures,
            callback_error_count=callback_error_count,
            degraded_reasons=tuple(sorted(degraded)),
        )

    # ── Internal assessors ──

    def _assess_tick_staleness(
        self,
        degraded: list[str],
        near_ts: float | None,
        far_ts: float | None,
        market_open: bool,
        now: float,
    ) -> None:
        if not market_open:
            return
        if near_ts is not None and (now - near_ts) * 1000 > self._tick_stale_after_ms:
            degraded.append(HealthReason.NEAR_TICK_STALE.value)
        if far_ts is not None and (now - far_ts) * 1000 > self._tick_stale_after_ms:
            degraded.append(HealthReason.FAR_TICK_STALE.value)

    def _assess_writer_health(
        self,
        degraded: list[str],
        consecutive_failures: int,
        last_success_at: float | None,
    ) -> None:
        if consecutive_failures >= 3:
            degraded.append(HealthReason.WRITER_FAILURES.value)
        if last_success_at is None:
            degraded.append(HealthReason.WRITER_NEVER_SUCCEEDED.value)

    def _assess_collector_state(
        self,
        degraded: list[str],
        generation: int,
        near_ts: float | None,
    ) -> None:
        if generation == 0:
            degraded.append(HealthReason.NO_TICKS_RECEIVED.value)
        elif near_ts is None:
            degraded.append(HealthReason.NEAR_TICK_MISSING.value)

    def _assess_callback_errors(self, degraded: list[str], error_count: int) -> None:
        if error_count > 0:
            degraded.append(HealthReason.CALLBACK_ERRORS.value)

    @staticmethod
    def _tick_age_ms(tick_time: float | None, now: float) -> int | None:
        if tick_time is None:
            return None
        return int((now - tick_time) * 1000)
