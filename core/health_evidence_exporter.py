#!/usr/bin/env python3
"""
Health evidence exporter — append-only JSONL sampler.

Periodically captures ``runtime.health().to_dict()`` and appends one
JSON line to ``{output_dir}/{product_code}_runtime_health.jsonl``.

Design:
  - Exporter is a separate adapter; it never calls ``os.open`` on
    the evaluator's path.
  - ``runtime.health()`` remains IO-free — all filesystem I/O is
    inside the sampler thread.
  - Single write failure is logged but does not stop the sampler.
  - Exporter has its own failure counter and last-success timestamp.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable


SCHEMA_VERSION = "1.0"


# ── Exporter Health ──

@dataclass(frozen=True)
class ExporterHealth:
    """Snapshot of sampler health at a point in time."""
    running: bool
    total_samples: int
    consecutive_failures: int
    last_error: str | None
    last_sample_at: datetime | None


# ── Sampler ──

class HealthEvidenceSampler:
    """Periodic health snapshot sampler (append-only JSONL).

    Usage::

        sampler = HealthEvidenceSampler(
            health_fn=runtime.health,
            product_code="MXF",
            output_dir="exports/market_data",
        )
        sampler.start()

        # ... later ...
        sampler.stop(final_sample=True)
    """

    def __init__(
        self,
        health_fn: Callable[[], Any],
        *,
        product_code: str = "MXF",
        output_dir: str = "exports/market_data",
        run_id: str | None = None,
        interval_sec: float = 30.0,
        git_commit: str | None = None,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._health_fn = health_fn
        self._product = product_code
        self._interval = interval_sec
        self._git_commit = git_commit or _detect_git_commit()
        self._logger = logger or logging.getLogger(self.__class__.__name__)
        self._clock = clock or time.time

        # Output path with optional run subdirectory
        if run_id:
            self._output_dir = os.path.join(output_dir, "soak", run_id)
        else:
            self._output_dir = output_dir
        self._output_path = os.path.join(
            self._output_dir, f"{product_code.lower()}_runtime_health.jsonl",
        )

        # Threading
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

        # Health
        self._lock = threading.Lock()
        self._total_samples: int = 0
        self._consecutive_failures: int = 0
        self._last_error: str | None = None
        self._last_sample_at: datetime | None = None

    # ── Properties ──

    @property
    def output_path(self) -> str:
        return self._output_path

    # ── Lifecycle ──

    def start(self) -> None:
        """Start the sampler thread.

        Safe to call multiple times — subsequent calls are no-ops.
        """
        if self._thread is not None and self._thread.is_alive():
            self._logger.warning("Sampler thread already running — start() is a no-op")
            return

        self._stop_event.clear()
        self._ensure_output_dir()

        def _loop() -> None:
            self._logger.info(
                "Sampler started (interval=%ss, output=%s)",
                self._interval, self._output_path,
            )
            # First sample immediately
            self._sample()
            while not self._stop_event.wait(self._interval):
                self._sample()

            # Final sample on stop
            self._sample()
            self._logger.info("Sampler stopped (total_samples=%d)", self._total_samples)

        self._thread = threading.Thread(
            target=_loop,
            name=f"health-sampler-{self._product}",
            daemon=False,
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0, *, final_sample: bool = True) -> None:
        """Signal sampler to stop and join the thread.

        Args:
            timeout: Max seconds to wait for thread join.
            final_sample: If True, the thread runs one more sample
                after receiving the stop signal.
        """
        if self._thread is None or not self._thread.is_alive():
            return

        self._stop_event.set()
        self._thread.join(timeout=timeout)

        if self._thread.is_alive():
            self._logger.warning("Sampler thread did not stop within %ss timeout", timeout)

        self._thread = None

    # ── Sampling ──

    def sample_once(self) -> dict[str, Any] | None:
        """Capture a single health snapshot and append to JSONL.

        Returns the snapshot dict, or None on failure.
        Designed to be testable without starting the thread.
        """
        self._ensure_output_dir()
        try:
            health_snapshot = self._health_fn()
            row = self._build_row(health_snapshot)
            self._append_row(row)
        except Exception as exc:
            self._record_failure(exc)
            self._logger.exception("Health sample failed")
            return None
        else:
            with self._lock:
                self._total_samples += 1
                self._consecutive_failures = 0
                self._last_error = None
                self._last_sample_at = datetime.now()
            return row

    def _sample(self) -> None:
        """Internal sampling wrapper (called from thread)."""
        self.sample_once()

    def _build_row(self, health_snapshot: Any) -> dict[str, Any]:
        """Build a single JSONL row from a health snapshot."""
        health_dict = health_snapshot.to_dict() if hasattr(health_snapshot, "to_dict") else health_snapshot

        return {
            "schema_version": SCHEMA_VERSION,
            "sampled_at": datetime.now().isoformat(),
            "product_code": self._product,
            "near_contract_code": health_dict.get("near_contract_code"),
            "far_contract_code": health_dict.get("far_contract_code"),
            "runtime_health": health_dict,
            "process_id": os.getpid(),
            "git_commit": self._git_commit,
        }

    def _append_row(self, row: dict[str, Any]) -> None:
        """Atomically append one JSON line to the output file."""
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with open(self._output_path, "a") as f:
            f.write(line)

    def _ensure_output_dir(self) -> None:
        os.makedirs(self._output_dir, exist_ok=True)

    def _record_failure(self, exc: Exception) -> None:
        with self._lock:
            self._consecutive_failures += 1
            self._last_error = f"{type(exc).__name__}: {exc}"

    # ── Health ──

    def sampler_health(self) -> ExporterHealth:
        with self._lock:
            return ExporterHealth(
                running=self._thread is not None and self._thread.is_alive(),
                total_samples=self._total_samples,
                consecutive_failures=self._consecutive_failures,
                last_error=self._last_error,
                last_sample_at=self._last_sample_at,
            )


def _detect_git_commit() -> str | None:
    """Detect current git commit hash."""
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return None
