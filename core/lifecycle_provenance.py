#!/usr/bin/env python3
"""
Lifecycle provenance event model.

A thread-safe, append-only event recorder that produces a single
deterministic event chain for every process lifecycle.

Design:
  - event_seq is monotonic per-process (atomic increment via Lock)
  - first-cause-wins: shutdown_reason is set once and never overridden
  - writer failure never affects the trading runtime (fail-open)
  - All events are appended to a JSONL file via a dedicated background writer
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ── Event Types ──

class LifecycleEventType(str, Enum):
    PROCESS_START = "PROCESS_START"
    QUOTE_SESSION_UP = "QUOTE_SESSION_UP"
    QUOTE_SESSION_DOWN_ERROR = "QUOTE_SESSION_DOWN_ERROR"
    QUOTE_CONNECT_FAILED = "QUOTE_CONNECT_FAILED"
    QUOTE_RECONNECTING = "QUOTE_RECONNECTING"
    QUOTE_RECONNECTED = "QUOTE_RECONNECTED"
    QUOTE_SUBSCRIPTION_OK = "QUOTE_SUBSCRIPTION_OK"
    SIGNAL_RECEIVED = "SIGNAL_RECEIVED"
    MAIN_LOOP_RETURNED = "MAIN_LOOP_RETURNED"
    SHUTDOWN_BEGIN = "SHUTDOWN_BEGIN"
    SHUTDOWN_COMPLETE = "SHUTDOWN_COMPLETE"
    TICK_RECEIVED = "TICK_RECEIVED"
    PM2_EXIT_METADATA = "PM2_EXIT_METADATA"


class ShutdownReason(str, Enum):
    SHIOAJI_SESSION_DOWN = "SHIOAJI_SESSION_DOWN"
    SHIOAJI_LOGIN_FAILURE = "SHIOAJI_LOGIN_FAILURE"
    SIGNAL_SIGTERM = "SIGNAL_SIGTERM"
    SIGNAL_SIGINT = "SIGNAL_SIGINT"
    SIGNAL_SIGHUP = "SIGNAL_SIGHUP"
    SIGNAL_OTHER = "SIGNAL_OTHER"
    MAIN_LOOP_RETURNED = "MAIN_LOOP_RETURNED"
    UNHANDLED_EXCEPTION = "UNHANDLED_EXCEPTION"
    UNKNOWN = "UNKNOWN"


class ShutdownInitiator(str, Enum):
    SIGNAL_HANDLER = "SIGNAL_HANDLER"
    SHIOAJI_EVENT_CALLBACK = "SHIOAJI_EVENT_CALLBACK"
    MAIN_LOOP = "MAIN_LOOP"
    EXCEPTION_HOOK = "EXCEPTION_HOOK"
    UNKNOWN = "UNKNOWN"


# ── Event Schema ──

@dataclass(frozen=True)
class LifecycleEvent:
    """A single event in the lifecycle event chain."""
    event_seq: int
    event_type: str
    observed_at: str  # ISO format
    process_uptime_sec: float | None

    # Shutdown fields (only populated for SHUTDOWN_BEGIN)
    shutdown_reason: str | None = None
    shutdown_initiator: str | None = None

    # Signal fields (only populated for SIGNAL_RECEIVED)
    signal_name: str | None = None
    signal_number: int | None = None

    # Quote event fields
    quote_event_code: int | None = None
    quote_event_name: str | None = None
    quote_info: str | None = None

    # Tick provenance
    last_tick_at: str | None = None
    last_tick_age_ms: float | None = None

    # PM2 metadata
    pm2_restart_count: int | None = None
    pm2_exit_code: int | None = None
    pm2_exit_signal: str | None = None
    pm2_process_id: int | None = None

    # Exception provenance
    exception_type: str | None = None
    exception_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result = {
            "event_seq": self.event_seq,
            "event_type": self.event_type,
            "observed_at": self.observed_at,
            "process_uptime_sec": self.process_uptime_sec,
        }
        for k, v in {
            "shutdown_reason": self.shutdown_reason,
            "shutdown_initiator": self.shutdown_initiator,
            "signal_name": self.signal_name,
            "signal_number": self.signal_number,
            "quote_event_code": self.quote_event_code,
            "quote_event_name": self.quote_event_name,
            "quote_info": self.quote_info,
            "last_tick_at": self.last_tick_at,
            "last_tick_age_ms": self.last_tick_age_ms,
            "pm2_restart_count": self.pm2_restart_count,
            "pm2_exit_code": self.pm2_exit_code,
            "pm2_exit_signal": self.pm2_exit_signal,
            "pm2_process_id": self.pm2_process_id,
            "exception_type": self.exception_type,
            "exception_message": self.exception_message,
        }.items():
            if v is not None:
                result[k] = v
        return result


# ── Recorder ──

class LifecycleRecorder:
    """Thread-safe, append-only lifecycle event recorder.

    Usage::

        recorder = LifecycleRecorder()
        recorder.record_event(LifecycleEventType.PROCESS_START)
        recorder.set_shutdown_cause(
            reason=ShutdownReason.SHIOAJI_SESSION_DOWN,
            initiator=ShutdownInitiator.SHIOAJI_EVENT_CALLBACK,
        )
    """

    def __init__(self, output_dir: str = "logs/lifecycle", logger: logging.Logger | None = None) -> None:
        self._lock = threading.Lock()
        self._event_seq: int = 0
        self._process_start = time.time()
        self._shutdown_reason: str | None = None
        self._shutdown_initiator: str | None = None
        self._last_tick_at: float | None = None
        self._output_dir = output_dir
        self._logger = logger or logging.getLogger(self.__class__.__name__)

        # Output path
        os.makedirs(output_dir, exist_ok=True)
        self._output_path = os.path.join(output_dir, "lifecycle_events.jsonl")

    # ── Properties ──

    @property
    def event_seq(self) -> int:
        return self._event_seq

    @property
    def shutdown_cause(self) -> tuple[str | None, str | None]:
        with self._lock:
            return self._shutdown_reason, self._shutdown_initiator

    @property
    def last_tick_at(self) -> float | None:
        return self._last_tick_at

    # ── Event Recording ──

    def record_event(self, event_type: LifecycleEventType | str, **fields: Any) -> int:
        """Record a lifecycle event. Returns the event_seq assigned.

        Thread-safe. Fail-open: logs and continues on error.
        """
        try:
            with self._lock:
                self._event_seq += 1
                seq = self._event_seq

            event = LifecycleEvent(
                event_seq=seq,
                event_type=event_type.value if isinstance(event_type, LifecycleEventType) else event_type,
                observed_at=datetime.now().isoformat(),
                process_uptime_sec=time.time() - self._process_start,
                **fields,
            )
            self._append_event(event)
            return seq
        except Exception as exc:
            self._logger.exception("Failed to record lifecycle event: %s", exc)
            return -1

    def record_tick(self) -> None:
        """Record that a tick was received (lightweight, stores timestamp only)."""
        self._last_tick_at = time.time()

    def set_shutdown_cause(self, reason: str | ShutdownReason,
                           initiator: str | ShutdownInitiator) -> None:
        """Set shutdown cause. First-cause-wins: subsequent calls are no-ops.

        Thread-safe.
        """
        r = reason.value if isinstance(reason, ShutdownReason) else reason
        i = initiator.value if isinstance(initiator, ShutdownInitiator) else initiator
        with self._lock:
            if self._shutdown_reason is not None:
                return  # first-cause-wins
            self._shutdown_reason = r
            self._shutdown_initiator = i

    def build_shutdown_event(self, **extra: Any) -> LifecycleEvent:
        """Build a SHUTDOWN_BEGIN event with the current shutdown cause."""
        reason, initiator = self.shutdown_cause
        last = self._last_tick_at
        last_age = ((time.time() - last) * 1000) if last is not None else None
        last_str = datetime.fromtimestamp(last).isoformat() if last is not None else None

        with self._lock:
            self._event_seq += 1
            seq = self._event_seq

        return LifecycleEvent(
            event_seq=seq,
            event_type=LifecycleEventType.SHUTDOWN_BEGIN.value,
            observed_at=datetime.now().isoformat(),
            process_uptime_sec=time.time() - self._process_start,
            shutdown_reason=reason or ShutdownReason.UNKNOWN.value,
            shutdown_initiator=initiator or ShutdownInitiator.UNKNOWN.value,
            last_tick_at=last_str,
            last_tick_age_ms=last_age,
            **extra,
        )

    def build_start_event(self, **extra: Any) -> LifecycleEvent:
        """Build a PROCESS_START event."""
        with self._lock:
            self._event_seq += 1
            seq = self._event_seq

        return LifecycleEvent(
            event_seq=seq,
            event_type=LifecycleEventType.PROCESS_START.value,
            observed_at=datetime.now().isoformat(),
            process_uptime_sec=0.0,
            **extra,
        )

    # ── Internal ──

    def _append_event(self, event: LifecycleEvent) -> None:
        """Append one JSON line. Fail-open: never raises."""
        try:
            line = json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n"
            with open(self._output_path, "a") as f:
                f.write(line)
        except Exception as exc:
            self._logger.exception("Lifecycle event write failed: %s", exc)

    # ── Shutdown ──

    def record_shutdown_complete(self) -> None:
        """Record the final SHUTDOWN_COMPLETE event before exit."""
        self.record_event(LifecycleEventType.SHUTDOWN_COMPLETE)

    def record_pm2_metadata(self, restart_count: int, exit_code: int | None,
                            exit_signal: str | None, pid: int) -> None:
        """Record PM2 exit metadata for the next process restart."""
        self.record_event(
            LifecycleEventType.PM2_EXIT_METADATA,
            pm2_restart_count=restart_count,
            pm2_exit_code=exit_code,
            pm2_exit_signal=exit_signal,
            pm2_process_id=pid,
        )
