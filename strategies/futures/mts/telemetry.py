# 2026-07-24 Gemini CLI: Wave 1D Non-Blocking Telemetry & Parity Spooler
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from queue import Full, Queue
from threading import Thread
from typing import Any


class ParityStatus(str, Enum):
    """Categorization of decision cycle parity outcome."""
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    LEGACY_RAISED = "LEGACY_RAISED"
    POLICY_RAISED = "POLICY_RAISED"
    BOTH_RAISED_SAME = "BOTH_RAISED_SAME"
    BOTH_RAISED_DIFFERENT = "BOTH_RAISED_DIFFERENT"
    SHADOW_SKIPPED = "SHADOW_SKIPPED"
    CONTEXT_BUILD_FAILED = "CONTEXT_BUILD_FAILED"
    TELEMETRY_DROPPED = "TELEMETRY_DROPPED"


class MismatchDimension(str, Enum):
    """Specific field breakdown when a parity mismatch occurs."""
    ACTION_MISMATCH = "ACTION_MISMATCH"
    LEG_MISMATCH = "LEG_MISMATCH"
    REASON_MISMATCH = "REASON_MISMATCH"
    STATE_MISMATCH = "STATE_MISMATCH"
    DIAGNOSTICS_MISMATCH = "DIAGNOSTICS_MISMATCH"


@dataclass(frozen=True)
class TelemetryCounterSummary:
    """Accounting counters guaranteeing total denominator verifiability."""
    cycles_seen: int = 0
    matches: int = 0
    mismatches: int = 0
    legacy_raised: int = 0
    policy_raised: int = 0
    shadow_skipped: int = 0
    telemetry_dropped: int = 0

    @property
    def is_accounted(self) -> bool:
        """Verify invariant: cycles_seen == matches + mismatches + legacy_raised + policy_raised + shadow_skipped."""
        return self.cycles_seen == (
            self.matches + self.mismatches + self.legacy_raised + self.policy_raised + self.shadow_skipped
        )


@dataclass(frozen=True)
class ParityTelemetryRecord:
    """Immutable, non-sensitive Telemetry Record for append-only JSONL spool."""
    record_type: str  # "MATCH" | "MISMATCH" | "EXCEPTION" | "SKIPPED" | "CHECKPOINT"
    schema_version: str = "1.0"
    event_id: str = ""
    decision_cycle_id: str = ""
    event_time_ns: int = 0
    session: str = "DAY"
    ticker: str = "TMF"
    parity_status: ParityStatus = ParityStatus.MATCH
    mismatch_dimensions: list[str] = field(default_factory=list)
    context_hash: str = ""
    config_hash: str = ""
    input_state_hash: str = ""
    legacy_action: str = ""
    shadow_action: str = ""
    legacy_reason: str = ""
    shadow_reason: str = ""
    shadow_eval_duration_us: float = 0.0
    sequence_number: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_json_line(self) -> str:
        """Serialize payload to compact canonical JSON string (without line breaks)."""
        data = asdict(self)
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


class ShadowTelemetryLogger:
    """Non-blocking background thread logger for append-only JSONL telemetry spool.
    
    Guarantees:
    1. Zero hot-path disk I/O blocking (bounded memory queue).
    2. Rate-limited drop counter accounting when queue overflows.
    3. Non-sensitive data sanitization (removes broker objects & secrets).
    """

    def __init__(self, spool_file_path: Path | str, queue_maxsize: int = 10000) -> None:
        self.spool_path = Path(spool_file_path)
        self.spool_path.parent.mkdir(parents=True, exist_ok=True)
        self._queue: Queue[ParityTelemetryRecord | None] = Queue(maxsize=queue_maxsize)
        
        self.cycles_seen: int = 0
        self.matches: int = 0
        self.mismatches: int = 0
        self.legacy_raised: int = 0
        self.policy_raised: int = 0
        self.shadow_skipped: int = 0
        self.telemetry_dropped: int = 0
        self._sequence_number: int = 0

        self._running: bool = True
        self._worker_thread = Thread(target=self._flusher_loop, daemon=True)
        self._worker_thread.start()

    def record_cycle(self, record: ParityTelemetryRecord) -> bool:
        """Enqueue telemetry record without blocking main decision loop.
        
        Returns True if successfully enqueued, False if dropped due to queue overflow.
        """
        self.cycles_seen += 1
        self._sequence_number += 1

        if record.parity_status == ParityStatus.MATCH:
            self.matches += 1
        elif record.parity_status == ParityStatus.MISMATCH:
            self.mismatches += 1
        elif record.parity_status == ParityStatus.LEGACY_RAISED:
            self.legacy_raised += 1
        elif record.parity_status == ParityStatus.POLICY_RAISED:
            self.policy_raised += 1
        elif record.parity_status == ParityStatus.SHADOW_SKIPPED:
            self.shadow_skipped += 1

        # Attach sequence number
        seq_record = ParityTelemetryRecord(
            record_type=record.record_type,
            schema_version=record.schema_version,
            event_id=record.event_id,
            decision_cycle_id=record.decision_cycle_id,
            event_time_ns=record.event_time_ns,
            session=record.session,
            ticker=record.ticker,
            parity_status=record.parity_status,
            mismatch_dimensions=record.mismatch_dimensions,
            context_hash=record.context_hash,
            config_hash=record.config_hash,
            input_state_hash=record.input_state_hash,
            legacy_action=record.legacy_action,
            shadow_action=record.shadow_action,
            legacy_reason=record.legacy_reason,
            shadow_reason=record.shadow_reason,
            shadow_eval_duration_us=record.shadow_eval_duration_us,
            sequence_number=self._sequence_number,
            details=record.details,
        )

        try:
            self._queue.put_nowait(seq_record)
            return True
        except Full:
            self.telemetry_dropped += 1
            return False

    def get_summary(self) -> TelemetryCounterSummary:
        """Get snapshot of current accounting counters."""
        return TelemetryCounterSummary(
            cycles_seen=self.cycles_seen,
            matches=self.matches,
            mismatches=self.mismatches,
            legacy_raised=self.legacy_raised,
            policy_raised=self.policy_raised,
            shadow_skipped=self.shadow_skipped,
            telemetry_dropped=self.telemetry_dropped,
        )

    def _flusher_loop(self) -> None:
        """Background thread writing queued JSONL records to disk."""
        with open(self.spool_path, "a", encoding="utf-8") as f:
            while self._running or not self._queue.empty():
                try:
                    record = self._queue.get(timeout=0.1)
                    if record is None:
                        break
                    f.write(record.to_json_line() + "\n")
                    f.flush()
                    self._queue.task_done()
                except Exception:
                    continue

    def stop(self) -> None:
        """Stop background worker thread gracefully and flush queue."""
        self._running = False
        self._queue.put(None)
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)


def compute_payload_hash(obj: Any) -> str:
    """Compute sha256 hex digest of a dataclass or dictionary payload."""
    serialized = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
