#!/usr/bin/env python3
"""Tests for core.background_snapshot_writer."""

import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any
import pytest

from core.background_snapshot_writer import (
    BackgroundSnapshotWriter,
    CsvSnapshotPersister,
    JsonStatePersister,
    SnapshotWriterHealth,
)


# ── Test Doubles ──

@dataclass(frozen=True)
class _FakeSnapshot:
    ticker: str
    generation: int = 0


class _FakeProvider:
    def __init__(self, ticker: str = "MTX", fail: bool = False) -> None:
        self._ticker = ticker
        self._fail = fail
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def snapshot_for_persistence(self) -> _FakeSnapshot | None:
        self._call_count += 1
        if self._fail:
            raise RuntimeError(f"provider failed for {self._ticker}")
        return _FakeSnapshot(ticker=self._ticker, generation=self._call_count)


class _FakeNoneProvider:
    """Returns None from snapshot."""

    def snapshot_for_persistence(self) -> None:
        return None


class _SpyPersister:
    def __init__(self, fail: bool = False) -> None:
        self.persisted: list[Any] = []
        self._fail = fail

    def persist(self, snapshot: Any) -> None:
        if self._fail:
            raise RuntimeError("persister failed")
        self.persisted.append(snapshot)


class _ExplodingPersister:
    def persist(self, snapshot: Any) -> None:
        raise RuntimeError("persister exploded")


# ── Health ──

class TestSnapshotWriterHealth:
    def test_default_health(self) -> None:
        h = SnapshotWriterHealth(running=False)
        assert h.running is False
        assert h.last_attempt_at is None
        assert h.last_success_at is None
        assert h.consecutive_failures == 0
        assert h.last_error is None

    def test_health_is_frozen(self) -> None:
        h = SnapshotWriterHealth(running=True)
        with pytest.raises(AttributeError):
            h.running = False  # type: ignore[misc]


# ── Initialization ──

class TestWriterInitialization:
    def test_requires_at_least_one_provider(self) -> None:
        with pytest.raises(ValueError, match="At least one provider"):
            BackgroundSnapshotWriter(providers=[], persisters=[_SpyPersister()])

    def test_requires_at_least_one_persister(self) -> None:
        with pytest.raises(ValueError, match="At least one persister"):
            BackgroundSnapshotWriter(providers=[_FakeProvider()], persisters=[])

    def test_initial_health_is_not_running(self) -> None:
        w = BackgroundSnapshotWriter(
            providers=[_FakeProvider()],
            persisters=[_SpyPersister()],
        )
        h = w.health()
        assert h.running is False
        assert h.consecutive_failures == 0


# ── flush_once — synchronous persistence ──

class TestFlushOnce:
    def test_flush_once_collects_and_persists(self) -> None:
        provider = _FakeProvider(ticker="MTX")
        persister = _SpyPersister()
        w = BackgroundSnapshotWriter(
            providers=[provider],
            persisters=[persister],
        )

        w.flush_once()

        assert len(persister.persisted) == 1
        assert persister.persisted[0].ticker == "MTX"
        assert persister.persisted[0].generation == 1

    def test_flush_once_multiple_providers(self) -> None:
        mtx = _FakeProvider(ticker="MTX")
        tmf = _FakeProvider(ticker="TMF")
        persister = _SpyPersister()
        w = BackgroundSnapshotWriter(
            providers=[mtx, tmf],
            persisters=[persister],
        )

        w.flush_once()

        assert len(persister.persisted) == 2
        assert persister.persisted[0].ticker == "MTX"
        assert persister.persisted[1].ticker == "TMF"

    def test_flush_once_multiple_persisters(self) -> None:
        provider = _FakeProvider(ticker="MTX")
        csv = _SpyPersister()
        jsn = _SpyPersister()
        w = BackgroundSnapshotWriter(
            providers=[provider],
            persisters=[csv, jsn],
        )

        w.flush_once()

        assert len(csv.persisted) == 1
        assert len(jsn.persisted) == 1

    def test_flush_once_provider_returns_none_skipped(self) -> None:
        provider = _FakeNoneProvider()
        persister = _SpyPersister()
        w = BackgroundSnapshotWriter(
            providers=[provider],
            persisters=[persister],
        )

        w.flush_once()

        assert len(persister.persisted) == 0

    def test_flush_once_records_attempt_and_success(self) -> None:
        w = BackgroundSnapshotWriter(
            providers=[_FakeProvider()],
            persisters=[_SpyPersister()],
        )

        w.flush_once()

        h = w.health()
        assert h.last_attempt_at is not None
        assert h.last_success_at is not None
        assert h.consecutive_failures == 0

    def test_flush_once_provider_failure_recorded(self) -> None:
        provider = _FakeProvider(fail=True)
        persister = _SpyPersister()
        w = BackgroundSnapshotWriter(
            providers=[provider],
            persisters=[persister],
        )

        w.flush_once()

        h = w.health()
        # Provider failed, persister shouldn't have received anything
        assert len(persister.persisted) == 0
        # But the failure IS recorded
        assert h.consecutive_failures > 0
        assert h.last_error is not None

    def test_flush_once_persister_failure_recorded(self) -> None:
        provider = _FakeProvider(ticker="MTX")
        persister = _ExplodingPersister()
        w = BackgroundSnapshotWriter(
            providers=[provider],
            persisters=[persister],
        )

        w.flush_once()

        h = w.health()
        assert h.consecutive_failures > 0

    def test_flush_once_continues_after_provider_failure(self) -> None:
        good = _FakeProvider(ticker="TMF")
        bad = _FakeProvider(ticker="MTX", fail=True)
        persister = _SpyPersister()
        w = BackgroundSnapshotWriter(
            providers=[good, bad],
            persisters=[persister],
        )

        w.flush_once()

        # Good provider's data should still be persisted
        assert len(persister.persisted) >= 1
        assert persister.persisted[0].ticker == "TMF"

    def test_cycle_based_failure_count(self) -> None:
        """Multiple failures in one cycle increment by at most 1."""
        bad_provider = _FakeProvider(ticker="MTX", fail=True)
        bad_persister = _ExplodingPersister()
        w = BackgroundSnapshotWriter(
            providers=[bad_provider],
            persisters=[bad_persister],
        )

        w.flush_once()
        assert w.health().consecutive_failures == 1


# ── Start / Stop lifecycle ──

class TestLifecycle:
    def test_start_starts_thread(self) -> None:
        w = BackgroundSnapshotWriter(
            providers=[_FakeProvider()],
            persisters=[_SpyPersister()],
        )

        w.start()
        assert w.health().running is True

        w.stop()
        assert w.health().running is False

    def test_start_is_idempotent(self) -> None:
        w = BackgroundSnapshotWriter(
            providers=[_FakeProvider()],
            persisters=[_SpyPersister()],
        )

        w.start()
        w.start()  # second call should be no-op
        assert w.health().running is True
        w.stop()

    def test_stop_before_start_is_noop(self) -> None:
        w = BackgroundSnapshotWriter(
            providers=[_FakeProvider()],
            persisters=[_SpyPersister()],
        )
        w.stop()  # should not raise

    def test_thread_runs_flush_cycles(self) -> None:
        provider = _FakeProvider(ticker="MTX")
        persister = _SpyPersister()
        w = BackgroundSnapshotWriter(
            providers=[provider],
            persisters=[persister],
            interval_sec=0.05,  # very fast for testing
        )

        w.start()
        time.sleep(0.15)  # let it run ~3 cycles
        w.stop()

        # Should have persisted at least once
        assert len(persister.persisted) >= 1
        assert persister.persisted[0].ticker == "MTX"

    def test_final_flush_on_stop(self) -> None:
        provider = _FakeProvider(ticker="MTX")
        persister = _SpyPersister()
        w = BackgroundSnapshotWriter(
            providers=[provider],
            persisters=[persister],
            interval_sec=3600,  # long interval — won't fire during test
        )

        w.start()
        time.sleep(0.05)
        count_before = len(persister.persisted)
        w.stop()  # final flush runs here

        # Final flush should have written one more snapshot
        assert len(persister.persisted) > count_before

    def test_reset_health(self) -> None:
        provider = _FakeProvider(fail=True)
        w = BackgroundSnapshotWriter(
            providers=[provider],
            persisters=[_SpyPersister()],
        )

        w.flush_once()
        assert w.health().consecutive_failures > 0

        w.reset_health()
        assert w.health().consecutive_failures == 0
        assert w.health().last_error is None


# ── Built-in Persisters (placeholders) ──

class TestBuiltinPersisters:
    def test_csv_persister_not_implemented(self) -> None:
        p = CsvSnapshotPersister(directory="/tmp")
        with pytest.raises(NotImplementedError):
            p.persist(_FakeSnapshot(ticker="MTX"))

    def test_json_persister_not_implemented(self) -> None:
        p = JsonStatePersister()
        with pytest.raises(NotImplementedError):
            p.persist(_FakeSnapshot(ticker="MTX"))
