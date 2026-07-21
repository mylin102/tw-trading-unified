#!/usr/bin/env python3
"""Tests for core.lifecycle_provenance."""

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from typing import Any

import pytest

from core.lifecycle_provenance import (
    LifecycleEvent,
    LifecycleEventType,
    LifecycleRecorder,
    ShutdownInitiator,
    ShutdownReason,
)


def _load_events(path: str) -> list[dict[str, Any]]:
    """Load all events from a lifecycle JSONL file."""
    if not os.path.exists(path):
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


class TestLifecycleEvent:
    def test_to_dict_includes_all_set_fields(self) -> None:
        event = LifecycleEvent(
            event_seq=1,
            event_type="PROCESS_START",
            observed_at="2026-07-22T01:00:00",
            process_uptime_sec=0.0,
            shutdown_reason="SIGNAL_SIGTERM",
            shutdown_initiator="SIGNAL_HANDLER",
        )
        d = event.to_dict()
        assert d["event_seq"] == 1
        assert d["event_type"] == "PROCESS_START"
        assert d["shutdown_reason"] == "SIGNAL_SIGTERM"
        assert d["shutdown_initiator"] == "SIGNAL_HANDLER"

    def test_to_dict_omits_none_fields(self) -> None:
        event = LifecycleEvent(
            event_seq=2,
            event_type="TICK_RECEIVED",
            observed_at="2026-07-22T01:00:00",
            process_uptime_sec=100.0,
        )
        d = event.to_dict()
        assert "shutdown_reason" not in d
        assert "signal_name" not in d
        assert "quote_event_code" not in d

    def test_is_frozen(self) -> None:
        event = LifecycleEvent(
            event_seq=1, event_type="TEST", observed_at="now", process_uptime_sec=1.0,
        )
        with pytest.raises(AttributeError):
            event.event_seq = 99  # type: ignore[misc]


class TestLifecycleRecorder:
    def test_record_event_returns_monotonic_seq(self) -> None:
        r = LifecycleRecorder(output_dir=tempfile.mkdtemp())
        s1 = r.record_event(LifecycleEventType.PROCESS_START)
        s2 = r.record_event(LifecycleEventType.SHUTDOWN_BEGIN)
        assert s1 == 1
        assert s2 == 2

    def test_record_event_appends_to_file(self) -> None:
        tmpdir = tempfile.mkdtemp()
        r = LifecycleRecorder(output_dir=tmpdir)
        r.record_event(LifecycleEventType.PROCESS_START)
        events = _load_events(r._output_path)
        assert len(events) == 1
        assert events[0]["event_type"] == "PROCESS_START"

    def test_multiple_events_monotonic_seq(self) -> None:
        tmpdir = tempfile.mkdtemp()
        r = LifecycleRecorder(output_dir=tmpdir)
        r.record_event(LifecycleEventType.PROCESS_START)
        r.record_event(LifecycleEventType.SHUTDOWN_BEGIN)
        r.record_event(LifecycleEventType.SHUTDOWN_COMPLETE)
        events = _load_events(r._output_path)
        seqs = [e["event_seq"] for e in events]
        assert seqs == [1, 2, 3]

    def test_first_cause_wins(self) -> None:
        r = LifecycleRecorder(output_dir=tempfile.mkdtemp())
        r.set_shutdown_cause(ShutdownReason.SHIOAJI_SESSION_DOWN, ShutdownInitiator.SHIOAJI_EVENT_CALLBACK)
        r.set_shutdown_cause(ShutdownReason.SIGNAL_SIGTERM, ShutdownInitiator.SIGNAL_HANDLER)
        reason, initiator = r.shutdown_cause
        assert reason == "SHIOAJI_SESSION_DOWN"
        assert initiator == "SHIOAJI_EVENT_CALLBACK"

    def test_build_shutdown_event_includes_cause(self) -> None:
        r = LifecycleRecorder(output_dir=tempfile.mkdtemp())
        r.set_shutdown_cause(ShutdownReason.SHIOAJI_SESSION_DOWN, ShutdownInitiator.SHIOAJI_EVENT_CALLBACK)
        event = r.build_shutdown_event()
        assert event.shutdown_reason == "SHIOAJI_SESSION_DOWN"
        assert event.shutdown_initiator == "SHIOAJI_EVENT_CALLBACK"
        assert event.event_type == "SHUTDOWN_BEGIN"

    def test_shutdown_without_cause_uses_unknown(self) -> None:
        r = LifecycleRecorder(output_dir=tempfile.mkdtemp())
        event = r.build_shutdown_event()
        assert event.shutdown_reason == "UNKNOWN"
        assert event.shutdown_initiator == "UNKNOWN"

    def test_record_tick_updates_last_tick_at(self) -> None:
        r = LifecycleRecorder(output_dir=tempfile.mkdtemp())
        assert r.last_tick_at is None
        r.record_tick()
        assert r.last_tick_at is not None

    def test_shutdown_event_includes_tick_age(self) -> None:
        r = LifecycleRecorder(output_dir=tempfile.mkdtemp())
        r.record_tick()
        time.sleep(0.01)
        event = r.build_shutdown_event()
        assert event.last_tick_at is not None
        assert event.last_tick_age_ms is not None
        assert event.last_tick_age_ms > 0

    def test_signal_event_includes_signal_name(self) -> None:
        tmpdir = tempfile.mkdtemp()
        r = LifecycleRecorder(output_dir=tmpdir)
        r.record_event(LifecycleEventType.SIGNAL_RECEIVED, signal_name="SIGTERM", signal_number=15)
        events = _load_events(r._output_path)
        assert events[0]["signal_name"] == "SIGTERM"
        assert events[0]["signal_number"] == 15

    def test_quote_event_includes_codes(self) -> None:
        tmpdir = tempfile.mkdtemp()
        r = LifecycleRecorder(output_dir=tmpdir)
        r.record_event(
            LifecycleEventType.QUOTE_SESSION_DOWN_ERROR,
            quote_event_code=1,
            quote_event_name="SESSION_DOWN_ERROR",
            quote_info="Connection lost",
        )
        events = _load_events(r._output_path)
        assert events[0]["quote_event_code"] == 1
        assert events[0]["quote_event_name"] == "SESSION_DOWN_ERROR"

    def test_pm2_metadata_recorded(self) -> None:
        tmpdir = tempfile.mkdtemp()
        r = LifecycleRecorder(output_dir=tmpdir)
        r.record_pm2_metadata(restart_count=22, exit_code=0, exit_signal=None, pid=12345)
        events = _load_events(r._output_path)
        assert events[0]["event_type"] == "PM2_EXIT_METADATA"
        assert events[0]["pm2_restart_count"] == 22
        assert events[0]["pm2_exit_code"] == 0
        assert events[0]["pm2_process_id"] == 12345
        assert "pm2_exit_signal" not in events[0]  # None → omitted

    def test_build_start_event_includes_extra(self) -> None:
        r = LifecycleRecorder(output_dir=tempfile.mkdtemp())
        event = r.build_start_event(pm2_restart_count=22, pm2_process_id=12345)
        assert event.event_type == "PROCESS_START"
        assert event.pm2_restart_count == 22
        assert event.pm2_process_id == 12345
        assert event.process_uptime_sec == 0.0

    def test_write_failure_does_not_raise(self) -> None:
        """Fail-open: write error is logged, not raised."""
        tmpdir = tempfile.mkdtemp()
        r = LifecycleRecorder(output_dir=tmpdir)
        # Write to an invalid path via internal method (skip __init__ makedirs)
        bad_path = os.path.join(tmpdir, "nonexistent_sub", "events.jsonl")
        r._output_path = bad_path
        # Should not raise
        r.record_event(LifecycleEventType.PROCESS_START)
        assert True

    def test_thread_safety(self) -> None:
        """Multiple threads recording events should not corrupt seq."""
        tmpdir = tempfile.mkdtemp()
        r = LifecycleRecorder(output_dir=tmpdir)
        n = 50
        errors = []

        def _worker() -> None:
            for _ in range(n):
                try:
                    r.record_event(LifecycleEventType.TICK_RECEIVED)
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        events = _load_events(r._output_path)
        assert len(events) == n * 4
        seqs = [e["event_seq"] for e in events]
        # Seq values must be unique
        assert len(set(seqs)) == len(seqs), f"Duplicate seqs: {seqs}"
        # Seq values must be in range 1..200
        assert min(seqs) == 1
        assert max(seqs) == n * 4

    def test_process_start_property(self) -> None:
        """event_seq starts at 0 before any recording."""
        r = LifecycleRecorder(output_dir=tempfile.mkdtemp())
        assert r.event_seq == 0
        r.record_event(LifecycleEventType.PROCESS_START)
        assert r.event_seq == 1

    def test_shutdown_complete_recorded(self) -> None:
        tmpdir = tempfile.mkdtemp()
        r = LifecycleRecorder(output_dir=tmpdir)
        r.record_shutdown_complete()
        events = _load_events(r._output_path)
        assert events[0]["event_type"] == "SHUTDOWN_COMPLETE"
