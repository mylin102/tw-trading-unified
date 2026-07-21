#!/usr/bin/env python3
"""Tests for core.market_data_health."""

import time
from datetime import datetime
import pytest

from core.market_data_health import (
    HealthReason,
    MarketDataHealthEvaluator,
    MarketDataRuntimeHealth,
    RuntimeHealthStatus,
)


# ── Fixtures ──

@pytest.fixture
def evaluator() -> MarketDataHealthEvaluator:
    return MarketDataHealthEvaluator(ticker="MTX", tick_stale_after_ms=5_000)


def _healthy_args(**overrides: object) -> dict:
    """Return a mostly-healthy baseline argument set."""
    now = time.time()
    args: dict = {
        "runtime_started": True,
        "registry_binding_count": 2,
        "near_contract_code": "MXFH6",
        "far_contract_code": "MXFI6",
        "collector_generation": 18452,
        "near_last_updated_at": now - 0.1,
        "far_last_updated_at": now - 0.3,
        "writer_running": True,
        "writer_last_success_at": now - 1.0,
        "writer_consecutive_failures": 0,
        "callback_error_count": 0,
        "market_expected_open": True,
    }
    args.update(overrides)
    return args


# ── MarketDataRuntimeHealth ──

class TestHealthSnapshot:
    def test_is_frozen(self) -> None:
        h = MarketDataRuntimeHealth(
            ticker="MTX",
            status=RuntimeHealthStatus.HEALTHY,
            observed_at=datetime.now(),
            runtime_started=True,
            registry_binding_count=2,
        )
        with pytest.raises(AttributeError):
            h.status = RuntimeHealthStatus.STOPPED  # type: ignore[misc]

    def test_to_dict_contains_all_fields(self) -> None:
        h = MarketDataRuntimeHealth(
            ticker="MTX",
            status=RuntimeHealthStatus.HEALTHY,
            observed_at=datetime(2026, 7, 21, 10, 30, 0),
            runtime_started=True,
            registry_binding_count=2,
            near_contract_code="MXFH6",
            far_contract_code="MXFI6",
            collector_generation=100,
            near_tick_age_ms=132,
            far_tick_age_ms=487,
            writer_running=True,
            writer_last_success_at=datetime(2026, 7, 21, 10, 29, 59),
            writer_consecutive_failures=0,
            callback_error_count=0,
            degraded_reasons=(),
        )
        d = h.to_dict()
        assert d["ticker"] == "MTX"
        assert d["status"] == "HEALTHY"
        assert d["observed_at"] == "2026-07-21T10:30:00"
        assert d["near_tick_age_ms"] == 132
        assert d["degraded_reasons"] == []

    def test_to_dict_no_none_datetime(self) -> None:
        h = MarketDataRuntimeHealth(
            ticker="MTX",
            status=RuntimeHealthStatus.STOPPED,
            observed_at=datetime.now(),
            runtime_started=False,
            registry_binding_count=0,
        )
        d = h.to_dict()
        assert d["writer_last_success_at"] is None
        assert d["degraded_reasons"] == []


# ── Health Evaluator ──

class TestEvaluatorHealthy:
    def test_healthy_when_all_good(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args())
        assert h.status == RuntimeHealthStatus.HEALTHY
        assert h.degraded_reasons == ()

    def test_healthy_generation_increasing(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(collector_generation=99999))
        assert h.status == RuntimeHealthStatus.HEALTHY

    def test_tick_age_computed(self, evaluator: MarketDataHealthEvaluator) -> None:
        now = time.time()
        h = evaluator.evaluate(**_healthy_args(
            near_last_updated_at=now - 0.1,
            far_last_updated_at=now - 0.5,
        ))
        assert h.near_tick_age_ms is not None
        assert h.near_tick_age_ms >= 90  # ~100ms
        assert h.far_tick_age_ms is not None
        assert h.far_tick_age_ms >= 490  # ~500ms


class TestEvaluatorStopped:
    def test_not_started_is_stopped(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(runtime_started=False))
        assert h.status == RuntimeHealthStatus.STOPPED
        assert not h.runtime_started


class TestEvaluatorUnhealthy:
    def test_writer_not_running_is_unhealthy(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(writer_running=False))
        assert h.status == RuntimeHealthStatus.UNHEALTHY
        assert h.status == RuntimeHealthStatus.UNHEALTHY
        assert HealthReason.WRITER_NOT_RUNNING.value in h.degraded_reasons


class TestEvaluatorDegraded:
    def test_near_tick_stale(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(
            near_last_updated_at=time.time() - 10,  # 10s stale
        ))
        assert h.status == RuntimeHealthStatus.DEGRADED
        assert HealthReason.NEAR_TICK_STALE.value in h.degraded_reasons

    def test_far_tick_stale(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(
            far_last_updated_at=time.time() - 10,
        ))
        assert h.status == RuntimeHealthStatus.DEGRADED
        assert HealthReason.FAR_TICK_STALE.value in h.degraded_reasons

    def test_both_legs_stale(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(
            near_last_updated_at=time.time() - 10,
            far_last_updated_at=time.time() - 15,
        ))
        assert h.status == RuntimeHealthStatus.DEGRADED
        assert HealthReason.NEAR_TICK_STALE.value in h.degraded_reasons
        assert HealthReason.FAR_TICK_STALE.value in h.degraded_reasons

    def test_market_closed_does_not_degrade_on_stale(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(
            near_last_updated_at=time.time() - 300,
            far_last_updated_at=time.time() - 300,
            market_expected_open=False,
        ))
        assert h.status == RuntimeHealthStatus.HEALTHY
        assert HealthReason.NEAR_TICK_STALE.value not in h.degraded_reasons

    def test_writer_consecutive_failures(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(writer_consecutive_failures=3))
        assert h.status == RuntimeHealthStatus.DEGRADED
        assert HealthReason.WRITER_FAILURES.value in h.degraded_reasons

    def test_writer_never_succeeded(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(writer_last_success_at=None))
        assert h.status == RuntimeHealthStatus.DEGRADED
        assert HealthReason.WRITER_NEVER_SUCCEEDED.value in h.degraded_reasons

    def test_no_ticks_received(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(collector_generation=0, near_last_updated_at=None))
        assert h.status == RuntimeHealthStatus.DEGRADED
        assert HealthReason.NO_TICKS_RECEIVED.value in h.degraded_reasons

    def test_near_never_received(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(
            collector_generation=5,
            near_last_updated_at=None,
        ))
        assert h.status == RuntimeHealthStatus.DEGRADED
        assert HealthReason.NEAR_TICK_MISSING.value in h.degraded_reasons

    def test_callback_errors_detected(self, evaluator: MarketDataHealthEvaluator) -> None:
        h = evaluator.evaluate(**_healthy_args(callback_error_count=1))
        assert h.status == RuntimeHealthStatus.DEGRADED
        assert HealthReason.CALLBACK_ERRORS.value in h.degraded_reasons

    def test_degraded_reasons_sorted(self, evaluator: MarketDataHealthEvaluator) -> None:
        """Multiple degradation factors produce a stable sort order."""
        h = evaluator.evaluate(**_healthy_args(
            near_last_updated_at=time.time() - 10,
            far_last_updated_at=time.time() - 15,
            writer_consecutive_failures=3,
            callback_error_count=2,
        ))
        reasons = h.degraded_reasons
        assert len(reasons) >= 3
        assert reasons == tuple(sorted(reasons))


class TestEvaluatorDeterminism:
    def test_same_inputs_same_output(self, evaluator: MarketDataHealthEvaluator) -> None:
        args = _healthy_args()
        # Replace dynamic timestamps with fixed values
        fixed_now = 1_000_000.0
        args["near_last_updated_at"] = fixed_now - 0.1
        args["far_last_updated_at"] = fixed_now - 0.3
        args["writer_last_success_at"] = fixed_now - 1.0

        # evaluate with time travel
        import core.market_data_health as mdh
        original_time = time.time
        try:
            time.time = lambda: fixed_now
            h1 = evaluator.evaluate(**args)
            h2 = evaluator.evaluate(**args)
            assert h1.status == h2.status
            assert h1.near_tick_age_ms == h2.near_tick_age_ms
            assert h1.degraded_reasons == h2.degraded_reasons
        finally:
            time.time = original_time


class TestEvaluatorIO:
    def test_evaluate_does_no_io(self, evaluator: MarketDataHealthEvaluator) -> None:
        """Health evaluation should never touch filesystem or network."""
        import os
        # Mock open to fail if called
        original_open = os.open
        call_count = 0
        def _no_open(*args: object, **kwargs: object) -> None:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("IO attempted during health evaluation")
        os.open = _no_open  # type: ignore[assignment]
        try:
            h = evaluator.evaluate(**_healthy_args())
            assert h.status is not None
        finally:
            os.open = original_open
        assert call_count == 0, f"os.open was called {call_count} time(s)"


class TestEvaluatorNoSideEffects:
    def test_evaluate_does_not_affect_callback_path(self, evaluator: MarketDataHealthEvaluator) -> None:
        """Running evaluate() should not modify any mutable runtime state."""
        original_gen = 18452
        h = evaluator.evaluate(**_healthy_args(collector_generation=original_gen))
        assert h.collector_generation == original_gen
        # The runtime's own generation counter should be unchanged
        # (tested by the caller, but the evaluator itself should never mutate inputs)


# ── Configurability ──

class TestEvaluatorConfig:
    def test_stale_threshold_respected(self) -> None:
        short_window = MarketDataHealthEvaluator(
            ticker="MTX", tick_stale_after_ms=1_000,
        )
        now = time.time()
        h = short_window.evaluate(**_healthy_args(
            near_last_updated_at=now - 1.5,  # 1500ms > 1000ms
        ))
        assert h.status == RuntimeHealthStatus.DEGRADED
        assert HealthReason.NEAR_TICK_STALE.value in h.degraded_reasons

    def test_low_threshold_not_stale(self) -> None:
        long_window = MarketDataHealthEvaluator(
            ticker="MTX", tick_stale_after_ms=10_000,
        )
        now = time.time()
        h = long_window.evaluate(**_healthy_args(
            near_last_updated_at=now - 2.0,  # 2000ms < 10000ms
        ))
        assert h.status == RuntimeHealthStatus.HEALTHY
