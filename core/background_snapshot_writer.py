#!/usr/bin/env python3
"""
Background snapshot writer — fixed-interval poll + atomic persistence.

Polling model (not event-driven):
  - No queue, no coalescing, no per-tick enqueue.
  - A background thread calls ``flush_once()`` at a fixed interval.
  - ``flush_once()`` iterates providers, collects snapshots, and writes.

Thread safety:
  - Writer never mutates provider/collector state.
  - Snapshot is produced under provider's own lock.
  - Filesystem I/O is exclusive to the writer thread.

Health:
  - ``health()`` returns a frozen dataclass with success/failure/error info.
  - Writer does NOT restart itself.  The supervisor (main watchdog) calls
    ``stop()`` + ``start()`` if ``health().consecutive_failures > N``.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence


# ── Health ──

@dataclass(frozen=True)
class SnapshotWriterHealth:
    """Snapshot of writer health at a point in time."""
    running: bool
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    consecutive_failures: int = 0
    last_error: str | None = None


# ── Snapshot Provider Protocol ──

class SnapshotProvider(Protocol):
    """Anything that can produce an immutable snapshot for persistence.

    Implemented by ``MarketDataCollector.snapshot_for_persistence()``.
    """

    def snapshot_for_persistence(self) -> Any:
        ...


# ── CSV / State Writers (pluggable) ──

class SnapshotPersister(Protocol):
    """Writes a single snapshot to persistent storage.

    Implementations must perform atomic writes
    (write to temp → ``os.replace()``).
    """

    def persist(self, snapshot: Any) -> None:
        ...


# ── Background Writer ──

class BackgroundSnapshotWriter:
    """Fixed-interval snapshot writer.

    Usage::

        writer = BackgroundSnapshotWriter(
            providers=[mtx_collector],
            persisters=[CsvPersister(), JsonStatePersister()],
            interval_sec=15.0,
        )
        writer.start()

        # ... later ...
        health = writer.health()
        writer.stop()

    The writer thread catches per-provider exceptions, logs them,
    increments ``consecutive_failures``, and continues to the next provider.
    After exhausting all providers it sleeps for the remaining interval
    before the next cycle.
    """

    def __init__(
        self,
        providers: Sequence[SnapshotProvider],
        persisters: Sequence[SnapshotPersister],
        *,
        interval_sec: float = 15.0,
        logger: logging.Logger | None = None,
    ) -> None:
        if not providers:
            raise ValueError("At least one provider is required")
        if not persisters:
            raise ValueError("At least one persister is required")

        self._providers = list(providers)
        self._persisters = list(persisters)
        self._interval = interval_sec
        self._logger = logger or logging.getLogger(self.__class__.__name__)

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Health — mutable, read under _health_lock
        self._health_lock = threading.Lock()
        self._last_attempt_at: datetime | None = None
        self._last_success_at: datetime | None = None
        self._consecutive_failures: int = 0
        self._last_error: str | None = None

    # ── Lifecycle ──

    def start(self) -> None:
        """Start the background writer thread.

        Safe to call multiple times — subsequent calls are no-ops
        when the thread is already running.
        """
        if self._thread is not None and self._thread.is_alive():
            self._logger.warning("Writer thread already running — start() is a no-op")
            return

        self._stop_event.clear()

        def _loop() -> None:
            self._logger.info(
                "Writer thread started (interval=%ss, providers=%d, persisters=%d)",
                self._interval, len(self._providers), len(self._persisters),
            )
            while not self._stop_event.wait(self._interval):
                self.flush_once()
            # Final flush on stop
            self._logger.info("Writer thread stopping — final flush")
            self.flush_once()
            self._logger.info("Writer thread stopped")

        self._thread = threading.Thread(
            target=_loop,
            name=f"snapshot-writer-{id(self)}",
            daemon=False,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0, *, final_flush: bool = True) -> None:
        """Signal the writer thread to stop and wait for it.

        Args:
            timeout: Max seconds to wait for thread join.
            final_flush: If True (default), the thread runs one more
                ``flush_once()`` after receiving the stop signal.
        """
        if self._thread is None or not self._thread.is_alive():
            return

        if final_flush:
            self._stop_event.set()  # thread will run final flush then exit
        else:
            self._stop_event.set()

        self._thread.join(timeout=timeout)
        if self._thread.is_alive():
            self._logger.warning("Writer thread did not stop within %ss timeout", timeout)
        self._thread = None

    # ── Core Persistence ──

    def flush_once(self) -> None:
        """Synchronous single flush cycle — collect snapshots + persist.

        This is the only method that performs I/O.
        Designed to be testable directly without starting the thread.

        Failure semantics (cycle-based):
          A single flush cycle increments ``consecutive_failures`` by at most 1,
          regardless of how many providers or persisters fail within the cycle.
        """
        now = datetime.now()
        errors: list[Exception] = []
        with self._health_lock:
            self._last_attempt_at = now

        for provider in self._providers:
            try:
                snapshot = provider.snapshot_for_persistence()
                if snapshot is None:
                    self._logger.debug("Provider %s returned None — skipping", type(provider).__name__)
                    continue

                for persister in self._persisters:
                    try:
                        persister.persist(snapshot)
                    except Exception as exc:
                        self._logger.exception(
                            "Persister %s failed for provider %s",
                            type(persister).__name__, type(provider).__name__,
                        )
                        errors.append(exc)
            except Exception as exc:
                self._logger.exception(
                    "Provider %s raised during snapshot", type(provider).__name__,
                )
                errors.append(exc)

        if errors:
            self._record_cycle_failure(errors[-1])
        else:
            with self._health_lock:
                self._last_success_at = datetime.now()
                self._consecutive_failures = 0
                self._last_error = None

    def _record_cycle_failure(self, last_error: Exception) -> None:
        """Increment consecutive-failure counter (cycle-based, at most +1 per flush)."""
        with self._health_lock:
            self._consecutive_failures += 1
            self._last_error = f"{type(last_error).__name__}: {last_error}"

    # ── Health ──

    def health(self) -> SnapshotWriterHealth:
        """Return a point-in-time snapshot of writer health."""
        with self._health_lock:
            return SnapshotWriterHealth(
                running=self._thread is not None and self._thread.is_alive(),
                last_attempt_at=self._last_attempt_at,
                last_success_at=self._last_success_at,
                consecutive_failures=self._consecutive_failures,
                last_error=self._last_error,
            )

    def reset_health(self) -> None:
        """Reset consecutive failure counter (called by supervisor after recovery)."""
        with self._health_lock:
            self._consecutive_failures = 0
            self._last_error = None


# ── Built-in Persisters ──

class CsvSnapshotPersister:
    """Write a snapshot to a CSV file via atomic replace.

    The snapshot must have a ``ticker`` attribute and the persister
    resolves the output path as ``{directory}/{ticker}_PAPER_indicators.csv``.
    """

    def __init__(self, directory: str, *, logger: logging.Logger | None = None) -> None:
        self._directory = directory
        self._logger = logger or logging.getLogger(self.__class__.__name__)

    def persist(self, snapshot: Any) -> None:
        """Not yet implemented — placeholder for CSV serialisation logic."""
        raise NotImplementedError("CsvSnapshotPersister.persist — to be implemented in PR 4")


class JsonStatePersister:
    """Write a snapshot to a JSON state file via atomic replace.

    Output path: ``/tmp/mts_position_state_{ticker.lower()}.json``
    """

    def __init__(self, output_dir: str = "/tmp", *, logger: logging.Logger | None = None) -> None:
        self._output_dir = output_dir
        self._logger = logger or logging.getLogger(self.__class__.__name__)

    def persist(self, snapshot: Any) -> None:
        """Not yet implemented — placeholder for JSON serialisation logic."""
        raise NotImplementedError("JsonStatePersister.persist — to be implemented in PR 4")
