"""
DTI-001A: Tick-Level Dynamics Telemetry Capture Hook.

Instrumentation only — zero impact on trading decisions.
Non-blocking bounded queue in callback → dedicated writer thread → per-generation JSONL.

Usage:
    capture = DynamicsCaptureHook(log_dir="logs/ticks/dynamics")
    capture.start()

    # In callback:
    try:
        capture.observe(exchange, tick, near_state=None, far_state=None)
    except Exception:
        logger.exception("DynamicsCaptureHook.observe failed")
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from queue import Queue, Full
from typing import Any

logger = logging.getLogger("dynamics_capture")

# ─── Schema ──────────────────────────────────────────────────────────

CAPTURE_SCHEMA_VERSION = "1.0.0"


def _get_git_commit_short() -> str:
    try:
        return (
            subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL)
            .decode("utf-8")
            .strip()
        )
    except Exception:
        return "unknown"


def _compute_generation_id() -> str:
    """<process_start_utc>-pid<pid>-<short_commit>

    Uses datetime.now() — NOT time.monotonic() — so the timestamp reflects
    actual UTC clock time, enabling restart boundary detection.
    """
    proc_start = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    pid = os.getpid()
    commit = _get_git_commit_short()
    return f"{proc_start}-pid{pid}-{commit}"


@dataclass
class TickCaptureEvent:
    """Immutable capture event — one per Shioaji tick."""

    # Schema
    schema_version: str = CAPTURE_SCHEMA_VERSION
    generation_id: str = field(default_factory=_compute_generation_id)

    # Event identity
    event_id: str = ""
    event_time: str = ""           # exchange/feed timestamp (ISO)
    received_at: str = ""          # system receive timestamp (ISO)
    processed_at: str = ""         # feature computation timestamp (ISO)
    source_sequence: int = 0       # monotonic counter from feed

    # Contract routing
    exchange: str = ""
    contract_code: str = ""
    is_near: bool = False
    is_far: bool = False
    trade_id: str = ""             # active trade at tick time, if any
    episode_id: str = ""           # entry-to-exit episode, if any
    session_id: str = ""           # "day" | "night"

    # Raw quote (near or far — one leg per event)
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    bid_size: int | None = None
    ask_size: int | None = None
    tick_age_ms: float | None = None  # event_time → received_at delta

    # Pair snapshot (state of the OTHER leg at observation time)
    pair_code: str = ""
    pair_bid: float | None = None
    pair_ask: float | None = None
    pair_last: float | None = None
    pair_age_ms: float | None = None
    pair_skew_ms: float | None = None  # |this.tick_age_ms - pair_age_ms|

    # Validity
    is_stale: bool = False
    stale_reason: str = ""

    # Derived status (DTI-001A: NOT_COMPUTED; filled by DTI-002+)
    derived_status: str = "NOT_COMPUTED"

    # Host metadata
    hostname: str = field(default_factory=platform.node)
    pid: int = field(default_factory=os.getpid)
    thread_id: int = field(default_factory=threading.get_ident)


# ─── Capture Hook ────────────────────────────────────────────────────

_EMPTY_CAPTURE = TickCaptureEvent()


class DynamicsCaptureHook:
    """
    Non-blocking tick capture with bounded queue and dedicated writer.

    Callback path:
        observe() → queue.put_nowait() → writer thread → JSONL flush

    Never blocks the calling thread. Never raises in callback path.
    """

    def __init__(
        self,
        log_dir: str | Path = "logs/ticks/dynamics",
        queue_maxsize: int = 10_000,
        flush_interval_ms: int = 500,
        flush_batch_size: int = 100,
        max_stale_age_ms: float = 5000.0,  # 5 seconds = stale
    ):
        self._log_dir = Path(log_dir)
        self._queue: Queue[TickCaptureEvent | None] = Queue(maxsize=queue_maxsize)
        self._queue_maxsize = queue_maxsize
        self._flush_interval_ms = flush_interval_ms
        self._flush_batch_size = flush_batch_size
        self._max_stale_age_ms = max_stale_age_ms

        self._writer_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._active_generation_id: str = ""

        # Telemetry counters
        self.captured_count: int = 0
        self.dropped_count: int = 0
        self.writer_error_count: int = 0
        self.last_write_time: float = 0.0
        self.writer_queue_depth: int = 0
        self._seq: int = 0

        self._log_dir.mkdir(parents=True, exist_ok=True)

    @property
    def active_generation_id(self) -> str:
        return self._active_generation_id

    # ── Lifecycle ────────────────────────────────────────────────

    def start(self) -> None:
        if self._writer_thread is not None and self._writer_thread.is_alive():
            logger.warning("DynamicsCaptureHook already started")
            return
        # Reset generation on start
        self._active_generation_id = _compute_generation_id()
        self._stop_event.clear()
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            name="dynamics-capture-writer",
            daemon=True,
        )
        self._writer_thread.start()
        logger.info(
            "DynamicsCaptureHook started | generation=%s | log_dir=%s",
            self._active_generation_id,
            self._log_dir,
        )

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        # Sentinel to wake writer (blocking send with timeout; best-effort)
        try:
            self._queue.put(None, timeout=1.0)
        except Full:
            logger.warning("Could not enqueue stop sentinel — queue full")
        if self._writer_thread and self._writer_thread.is_alive():
            self._writer_thread.join(timeout=timeout)
        logger.info(
            "DynamicsCaptureHook stopped | captured=%d dropped=%d errors=%d",
            self.captured_count,
            self.dropped_count,
            self.writer_error_count,
        )

    # ── Callback-safe observe ────────────────────────────────────

    def observe(
        self,
        exchange: str,
        tick: Any,
        *,
        near_state: dict | None = None,
        far_state: dict | None = None,
        trade_id: str = "",
        episode_id: str = "",
        session_id: str = "",
    ) -> None:
        """
        Non-blocking, best-effort, exception-contained capture.

        Called from Shioaji tick callback. NEVER raises.
        """
        try:
            self._observe_impl(
                exchange, tick,
                near_state=near_state,
                far_state=far_state,
                trade_id=trade_id,
                episode_id=episode_id,
                session_id=session_id,
            )
        except Exception:
            logger.exception("DynamicsCaptureHook._observe_impl failed — capture skipped")

    def _observe_impl(
        self,
        exchange: str,
        tick: Any,
        *,
        near_state: dict | None,
        far_state: dict | None,
        trade_id: str,
        episode_id: str,
        session_id: str,
    ) -> None:
        now = datetime.now(timezone.utc)

        # Extract tick fields (Shioaji TickFOPv1 or TickFOPv1Optimized)
        code = str(getattr(tick, "code", "")).strip().upper()
        event_time_str = str(getattr(tick, "datetime", "") or "")
        close = _safe_float(getattr(tick, "close", None))
        bid = _safe_float(getattr(tick, "buy_price", None))
        ask = _safe_float(getattr(tick, "sell_price", None))
        bid_size = _safe_int(getattr(tick, "bid_volume", None))
        ask_size = _safe_int(getattr(tick, "ask_volume", None))

        # Parse event_time
        event_dt = _parse_ts(event_time_str)
        event_time_iso = event_dt.isoformat() if event_dt else ""
        received_at_iso = now.isoformat()

        # Compute tick age
        tick_age_ms: float | None = None
        if event_dt:
            tick_age_ms = (now - event_dt).total_seconds() * 1000.0

        # Determine near/far from state hints
        is_near = False
        is_far = False
        pair_code = ""
        pair_bid: float | None = None
        pair_ask: float | None = None
        pair_last: float | None = None
        pair_age_ms: float | None = None
        pair_skew_ms: float | None = None

        if near_state and near_state.get("code", "").upper() == code:
            is_near = True
            if far_state:
                pair_code = far_state.get("code", "")
                pair_bid = _safe_float(far_state.get("bid"))
                pair_ask = _safe_float(far_state.get("ask"))
                pair_last = _safe_float(far_state.get("last"))
                pair_age_ms = far_state.get("age_ms")
        elif far_state and far_state.get("code", "").upper() == code:
            is_far = True
            if near_state:
                pair_code = near_state.get("code", "")
                pair_bid = _safe_float(near_state.get("bid"))
                pair_ask = _safe_float(near_state.get("ask"))
                pair_last = _safe_float(near_state.get("last"))
                pair_age_ms = near_state.get("age_ms")

        if pair_age_ms is not None and tick_age_ms is not None:
            pair_skew_ms = abs(tick_age_ms - pair_age_ms)

        # Stale detection
        is_stale = tick_age_ms is not None and tick_age_ms > self._max_stale_age_ms
        stale_reason = ""
        if is_stale:
            stale_reason = f"tick_age_ms={tick_age_ms:.0f} > max={self._max_stale_age_ms:.0f}"

        # Sequence
        self._seq += 1
        event_id = f"{self._active_generation_id}-seq{self._seq:08d}"

        event = TickCaptureEvent(
            generation_id=self._active_generation_id,
            event_id=event_id,
            event_time=event_time_iso,
            received_at=received_at_iso,
            processed_at=datetime.now(timezone.utc).isoformat(),
            source_sequence=self._seq,
            exchange=str(exchange) if exchange else "",
            contract_code=code,
            is_near=is_near,
            is_far=is_far,
            trade_id=trade_id,
            episode_id=episode_id,
            session_id=session_id,
            bid=bid,
            ask=ask,
            last=close,
            bid_size=bid_size,
            ask_size=ask_size,
            tick_age_ms=round(tick_age_ms, 2) if tick_age_ms is not None else None,
            pair_code=pair_code,
            pair_bid=pair_bid,
            pair_ask=pair_ask,
            pair_last=pair_last,
            pair_age_ms=round(pair_age_ms, 2) if pair_age_ms is not None else None,
            pair_skew_ms=round(pair_skew_ms, 2) if pair_skew_ms is not None else None,
            is_stale=is_stale,
            stale_reason=stale_reason,
        )

        # Non-blocking enqueue
        try:
            self._queue.put_nowait(event)
            self.captured_count += 1
        except Full:
            self.dropped_count += 1
            if self.dropped_count <= 10 or self.dropped_count % 1000 == 0:
                logger.warning(
                    "Capture queue full — dropped event #%d (total dropped: %d)",
                    self._seq,
                    self.dropped_count,
                )

    # ── Writer thread ────────────────────────────────────────────

    def _writer_loop(self) -> None:
        buffer: list[str] = []
        last_flush = time.monotonic()

        session_date = datetime.now(timezone.utc).strftime("%Y%m%d")
        session_dir = self._log_dir / session_date
        session_dir.mkdir(parents=True, exist_ok=True)

        file_path = session_dir / f"dynamics_{session_date}_{self._active_generation_id}.jsonl"
        fh = open(file_path, "a", encoding="utf-8")
        logger.info("Capture file opened: %s", file_path)

        while not self._stop_event.is_set():
            try:
                # Blocking get with timeout for flush responsiveness
                event = self._queue.get(timeout=self._flush_interval_ms / 1000.0)
            except Exception:
                # Timeout — flush buffer
                if buffer:
                    self._flush_buffer(fh, buffer)
                    buffer.clear()
                    last_flush = time.monotonic()
                continue

            if event is None:
                # Sentinel received — stop
                break

            buffer.append(json.dumps(asdict(event), ensure_ascii=False, default=str))

            now = time.monotonic()
            if (
                len(buffer) >= self._flush_batch_size
                or (now - last_flush) >= (self._flush_interval_ms / 1000.0)
            ):
                self._flush_buffer(fh, buffer)
                buffer.clear()
                last_flush = now

            self.writer_queue_depth = self._queue.qsize()

        # Final flush
        if buffer:
            self._flush_buffer(fh, buffer)
        fh.close()
        logger.info("Capture file closed: %s (dropped=%d)", file_path, self.dropped_count)

    def _flush_buffer(self, fh, buffer: list[str]) -> None:
        try:
            fh.write("\n".join(buffer) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
            self.last_write_time = time.time()
        except Exception:
            self.writer_error_count += 1
            logger.exception("Capture writer flush failed")


# ─── Helpers ────────────────────────────────────────────────────────

def _safe_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _safe_int(v: Any) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _parse_ts(ts_str: str) -> datetime | None:
    """Parse Shioaji timestamp string -> datetime."""
    if not ts_str:
        return None
    for fmt in [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
    ]:
        try:
            return datetime.strptime(ts_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None
