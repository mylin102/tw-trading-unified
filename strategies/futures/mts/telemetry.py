# 2026-07-24 Gemini CLI: Wave 1D Dual-Track Accounting & Process-Safe Telemetry Logger
import hashlib
import json
import os
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import Enum
from pathlib import Path
from queue import Full, Queue
from threading import Thread
from typing import Any


class ParityStatus(str, Enum):
    """Categorization of decision cycle evaluation parity outcome."""
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    LEGACY_RAISED_ONLY = "LEGACY_RAISED_ONLY"
    POLICY_RAISED_ONLY = "POLICY_RAISED_ONLY"
    BOTH_RAISED_SAME = "BOTH_RAISED_SAME"
    BOTH_RAISED_DIFFERENT = "BOTH_RAISED_DIFFERENT"
    SHADOW_SKIPPED = "SHADOW_SKIPPED"
    CONTEXT_BUILD_FAILED = "CONTEXT_BUILD_FAILED"


class MismatchDimension(str, Enum):
    """Field breakdown when a parity mismatch occurs."""
    ACTION_MISMATCH = "ACTION_MISMATCH"
    LEG_MISMATCH = "LEG_MISMATCH"
    REASON_MISMATCH = "REASON_MISMATCH"
    STATE_MISMATCH = "STATE_MISMATCH"
    DIAGNOSTICS_MISMATCH = "DIAGNOSTICS_MISMATCH"


@dataclass(frozen=True)
class EvaluationAccountingSummary:
    """Accounting counters for evaluation outcomes.
    
    Invariant Equation 1:
    cycles_seen == matches + mismatches + legacy_raised_only + policy_raised_only +
                  both_raised_same + both_raised_different + shadow_skipped + context_build_failed
    """
    cycles_seen: int = 0
    matches: int = 0
    mismatches: int = 0
    legacy_raised_only: int = 0
    policy_raised_only: int = 0
    both_raised_same: int = 0
    both_raised_different: int = 0
    shadow_skipped: int = 0
    context_build_failed: int = 0

    @property
    def is_accounted(self) -> bool:
        """Verify Equation 1 invariant: every decision cycle enters EXACTLY ONE evaluation category."""
        expected_total = (
            self.matches
            + self.mismatches
            + self.legacy_raised_only
            + self.policy_raised_only
            + self.both_raised_same
            + self.both_raised_different
            + self.shadow_skipped
            + self.context_build_failed
        )
        return self.cycles_seen == expected_total


@dataclass(frozen=True)
class TelemetryDeliveryAccountingSummary:
    """Accounting counters for non-blocking telemetry log delivery.
    
    Invariant Equation 2:
    telemetry_enqueued == telemetry_written + telemetry_dropped + telemetry_pending
    """
    telemetry_enqueued: int = 0
    telemetry_written: int = 0
    telemetry_dropped: int = 0
    telemetry_pending: int = 0

    @property
    def is_accounted(self) -> bool:
        """Verify Equation 2 invariant: enqueued == written + dropped + pending."""
        return self.telemetry_enqueued == (self.telemetry_written + self.telemetry_dropped + self.telemetry_pending)


@dataclass(frozen=True)
class ParityTelemetryRecord:
    """Immutable, sanitized Telemetry Record for append-only JSONL spool."""
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
        """Serialize payload to compact canonical JSON string without line breaks."""
        data = canonicalize(asdict(self))
        return json.dumps(data, separators=(",", ":"), ensure_ascii=False)


def canonicalize(obj: Any) -> Any:
    """Recursively convert object into canonical representation (Decimal -> str, Enum -> str, dict sorted)."""
    if isinstance(obj, Decimal):
        return str(obj)
    elif isinstance(obj, Enum):
        return obj.value
    elif isinstance(obj, dict):
        return {k: canonicalize(v) for k, v in sorted(obj.items())}
    elif isinstance(obj, (list, tuple)):
        return [canonicalize(item) for item in obj]
    return obj


def compute_canonical_hash(obj: Any) -> str:
    """Compute sha256 hex digest of a canonicalized payload."""
    canonical_data = canonicalize(obj)
    serialized = json.dumps(canonical_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]


class ProcessSafeTelemetryLogger:
    """Process-safe, non-blocking telemetry spooler with process-isolated file naming and dual-track accounting.
    
    File Naming:
    data/telemetry/mts_parity/raw/<deployment_id>_<pid>_<start_ns>.jsonl
    """

    def __init__(
        self,
        base_dir: Path | str,
        deployment_id: str = "default-deploy",
        queue_maxsize: int = 10000,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.deployment_id = deployment_id
        self.start_ns = time.time_ns()
        self.pid = os.getpid()

        self.spool_path = self.base_dir / f"{self.deployment_id}_{self.pid}_{self.start_ns}.jsonl"

        self._queue: Queue[ParityTelemetryRecord | None] = Queue(maxsize=queue_maxsize)
        
        # Evaluation Counters (Equation 1)
        self.cycles_seen: int = 0
        self.matches: int = 0
        self.mismatches: int = 0
        self.legacy_raised_only: int = 0
        self.policy_raised_only: int = 0
        self.both_raised_same: int = 0
        self.both_raised_different: int = 0
        self.shadow_skipped: int = 0
        self.context_build_failed: int = 0

        # Delivery Counters (Equation 2)
        self.telemetry_enqueued: int = 0
        self.telemetry_written: int = 0
        self.telemetry_dropped: int = 0

        self._sequence_number: int = 0
        self._running: bool = True
        self._worker_thread = Thread(target=self._flusher_loop, daemon=True)
        self._worker_thread.start()

    def record_cycle(self, record: ParityTelemetryRecord) -> bool:
        """Record decision cycle evaluation and enqueue telemetry record without blocking main loop."""
        self.cycles_seen += 1
        self._sequence_number += 1

        # Increment Equation 1 evaluation counter
        status = record.parity_status
        if status == ParityStatus.MATCH:
            self.matches += 1
        elif status == ParityStatus.MISMATCH:
            self.mismatches += 1
        elif status == ParityStatus.LEGACY_RAISED_ONLY:
            self.legacy_raised_only += 1
        elif status == ParityStatus.POLICY_RAISED_ONLY:
            self.policy_raised_only += 1
        elif status == ParityStatus.BOTH_RAISED_SAME:
            self.both_raised_same += 1
        elif status == ParityStatus.BOTH_RAISED_DIFFERENT:
            self.both_raised_different += 1
        elif status == ParityStatus.SHADOW_SKIPPED:
            self.shadow_skipped += 1
        elif status == ParityStatus.CONTEXT_BUILD_FAILED:
            self.context_build_failed += 1

        # Enqueue for delivery
        self.telemetry_enqueued += 1
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

    def get_evaluation_summary(self) -> EvaluationAccountingSummary:
        """Get snapshot of Equation 1 Evaluation Accounting Summary."""
        return EvaluationAccountingSummary(
            cycles_seen=self.cycles_seen,
            matches=self.matches,
            mismatches=self.mismatches,
            legacy_raised_only=self.legacy_raised_only,
            policy_raised_only=self.policy_raised_only,
            both_raised_same=self.both_raised_same,
            both_raised_different=self.both_raised_different,
            shadow_skipped=self.shadow_skipped,
            context_build_failed=self.context_build_failed,
        )

    def get_delivery_summary(self) -> TelemetryDeliveryAccountingSummary:
        """Get snapshot of Equation 2 Delivery Accounting Summary."""
        pending = self._queue.qsize()
        return TelemetryDeliveryAccountingSummary(
            telemetry_enqueued=self.telemetry_enqueued,
            telemetry_written=self.telemetry_written,
            telemetry_dropped=self.telemetry_dropped,
            telemetry_pending=pending,
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
                    self.telemetry_written += 1
                    self._queue.task_done()
                except Exception:
                    continue

    def stop(self) -> None:
        """Stop background worker thread gracefully and flush pending records."""
        self._running = False
        self._queue.put(None)
        if self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
