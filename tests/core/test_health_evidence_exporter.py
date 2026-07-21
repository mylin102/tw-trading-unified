#!/usr/bin/env python3
"""Tests for core.health_evidence_exporter."""

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
import pytest

from core.health_evidence_exporter import (
    ExporterHealth,
    HealthEvidenceSampler,
    _detect_git_commit,
)


# ── Test Doubles ──

class _FakeHealth:
    def __init__(self, status: str = "HEALTHY", generation: int = 100,
                 observed_at: str | None = None) -> None:
        self._status = status
        self._generation = generation
        self._observed_at = observed_at or "2026-07-21T10:00:00"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": "MXF",
            "status": self._status,
            "observed_at": self._observed_at,
            "collector_generation": self._generation,
            "near_tick_age_ms": 132,
            "far_tick_age_ms": 487,
            "writer_consecutive_failures": 0,
            "callback_error_count": 0,
            "degraded_reasons": [],
        }


class _FailingHealth:
    def to_dict(self) -> dict[str, Any]:
        raise RuntimeError("health check failed")


class _ManualClock:
    """Injected clock for deterministic testing."""

    def __init__(self, start: float = 1000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


# ── ExporterHealth ──

class TestExporterHealth:
    def test_default_health(self) -> None:
        h = ExporterHealth(
            running=False,
            total_samples=0,
            consecutive_failures=0,
            last_error=None,
            last_sample_at=None,
        )
        assert h.running is False
        assert h.total_samples == 0

    def test_is_frozen(self) -> None:
        h = ExporterHealth(
            running=True, total_samples=5, consecutive_failures=0,
            last_error=None, last_sample_at=None,
        )
        with pytest.raises(AttributeError):
            h.running = False  # type: ignore[misc]


# ── HealthEvidenceSampler ──

class TestSamplerInitialization:
    def test_output_path_contains_product(self) -> None:
        sampler = HealthEvidenceSampler(
            health_fn=lambda: _FakeHealth(),
            product_code="MXF",
        )
        assert "mxf" in sampler.output_path.lower()
        assert sampler.output_path.endswith(".jsonl")

    def test_initial_health(self) -> None:
        sampler = HealthEvidenceSampler(health_fn=lambda: _FakeHealth())
        h = sampler.sampler_health()
        assert h.running is False
        assert h.total_samples == 0


class TestSampleOnce:
    def test_sample_once_returns_dict(self) -> None:
        sampler = HealthEvidenceSampler(health_fn=lambda: _FakeHealth())
        result = sampler.sample_once()
        assert result is not None
        assert result["schema_version"] == "1.0"
        assert result["product_code"] == "MXF"
        assert result["runtime_health"]["status"] == "HEALTHY"

    def test_sample_once_appends_to_file(self) -> None:
        tmpdir = tempfile.mkdtemp()
        sampler = HealthEvidenceSampler(
            health_fn=lambda: _FakeHealth(),
            output_dir=tmpdir,
        )
        result = sampler.sample_once()

        # File should exist with one line
        assert os.path.exists(sampler.output_path)
        with open(sampler.output_path) as f:
            lines = f.readlines()
        assert len(lines) == 1
        parsed = json.loads(lines[0])
        assert parsed["schema_version"] == "1.0"
        assert parsed["runtime_health"]["status"] == "HEALTHY"

    def test_two_samples_two_lines(self) -> None:
        tmpdir = tempfile.mkdtemp()
        sampler = HealthEvidenceSampler(
            health_fn=lambda: _FakeHealth(),
            output_dir=tmpdir,
        )
        sampler.sample_once()
        sampler.sample_once()

        with open(sampler.output_path) as f:
            lines = f.readlines()
        assert len(lines) == 2

    def test_sample_once_increments_counter(self) -> None:
        sampler = HealthEvidenceSampler(health_fn=lambda: _FakeHealth())
        assert sampler.sampler_health().total_samples == 0
        sampler.sample_once()
        assert sampler.sampler_health().total_samples == 1
        sampler.sample_once()
        assert sampler.sampler_health().total_samples == 2

    def test_health_failure_returns_none(self) -> None:
        sampler = HealthEvidenceSampler(health_fn=lambda: _FailingHealth())
        result = sampler.sample_once()
        assert result is None
        assert sampler.sampler_health().consecutive_failures == 1
        assert sampler.sampler_health().last_error is not None

    def test_failure_does_not_stop_sampling(self) -> None:
        """After a failure, subsequent samples still work."""
        call_count = 0

        def _alternating():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("first fail")
            return _FakeHealth()

        sampler = HealthEvidenceSampler(health_fn=_alternating)
        assert sampler.sample_once() is None  # fails
        assert sampler.sample_once() is not None  # succeeds
        assert sampler.sampler_health().consecutive_failures == 0  # reset on success

    def test_snapshot_not_mutated(self) -> None:
        """Exporter should not modify the health snapshot."""
        original = _FakeHealth()
        snapshot_before = original.to_dict()
        sampler = HealthEvidenceSampler(health_fn=lambda: original)
        sampler.sample_once()
        snapshot_after = original.to_dict()
        assert snapshot_before == snapshot_after


class TestLifecycle:
    def test_start_and_stop(self) -> None:
        tmpdir = tempfile.mkdtemp()
        clock = _ManualClock()

        sampler = HealthEvidenceSampler(
            health_fn=lambda: _FakeHealth(),
            output_dir=tmpdir,
            interval_sec=0.05,
            clock=clock,
        )

        sampler.start()
        assert sampler.sampler_health().running is True

        # Let it sample a couple times
        clock.advance(0.12)
        time.sleep(0.05)  # allow thread to process events

        sampler.stop(final_sample=True)
        assert sampler.sampler_health().running is False

        # Should have sampled at least 2 times (1 immediate + 1 interval + 1 final)
        with open(sampler.output_path) as f:
            lines = f.readlines()
        assert len(lines) >= 2

    def test_start_is_idempotent(self) -> None:
        sampler = HealthEvidenceSampler(
            health_fn=lambda: _FakeHealth(),
            interval_sec=3600,
        )
        sampler.start()
        sampler.start()  # second call should be no-op
        assert sampler.sampler_health().running is True
        sampler.stop()

    def test_stop_before_start_is_noop(self) -> None:
        sampler = HealthEvidenceSampler(health_fn=lambda: _FakeHealth())
        sampler.stop()  # should not raise

    def test_final_sample_on_stop(self) -> None:
        tmpdir = tempfile.mkdtemp()
        clock = _ManualClock()
        sampler = HealthEvidenceSampler(
            health_fn=lambda: _FakeHealth(),
            output_dir=tmpdir,
            interval_sec=3600,  # won't fire during test
            clock=clock,
        )
        sampler.start()
        sampler.stop(final_sample=True)
        with open(sampler.output_path) as f:
            lines = f.readlines()
        # 1 immediate + 1 final = 2
        assert len(lines) == 2


class TestGitDetection:
    def test_detect_git_commit_returns_string(self) -> None:
        commit = _detect_git_commit()
        # In CI or without git, this can be None
        if commit is not None:
            assert len(commit) >= 7


class TestProvenance:
    def test_row_contains_provenance(self) -> None:
        sampler = HealthEvidenceSampler(health_fn=lambda: _FakeHealth())
        result = sampler.sample_once()
        assert result is not None
        assert "process_id" in result
        assert isinstance(result["process_id"], int)
        assert result["process_id"] > 0
        # git_commit might be None in test env
        assert "git_commit" in result

    def test_reason_codes_preserved(self) -> None:
        sampler = HealthEvidenceSampler(
            health_fn=lambda: _FakeHealth(),
        )
        result = sampler.sample_once()
        assert result is not None
        reasons = result["runtime_health"]["degraded_reasons"]
        assert isinstance(reasons, list)
