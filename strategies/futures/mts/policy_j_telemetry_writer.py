# 2026-07-26 Gemini CLI: Wave J1.5-B Policy J Telemetry JSONL Writer
import logging
from pathlib import Path
from typing import Optional
from core.runtime_paths import runtime_path

from strategies.futures.mts.policy_j_telemetry_schema import PolicyJShadowSnapshot

logger = logging.getLogger(__name__)


class PolicyJTelemetryWriter:
    """
    Append-only JSONL Telemetry Writer for Policy J Shadow Mode.
    100% isolated — I/O errors are caught and logged without throwing or interrupting the main trading loop.
    """

    def __init__(self, export_dir: Optional[Path] = None):
        if export_dir is None:
            project_root = Path(__file__).parent.parent.parent.parent
            export_dir = Path(runtime_path("exports", "telemetry", "policy_j"))
        self.export_dir = Path(export_dir)
        self.write_error_count = 0
        self.records_written = 0
        self.consecutive_failures = 0
        self.last_success_time: Optional[str] = None
        self.last_error_time: Optional[str] = None

    def resolve_target_file(self, date_str: str) -> Path:
        """Resolve session date-based target file path (e.g. policy_j_shadow_20260726.jsonl)."""
        self.export_dir.mkdir(parents=True, exist_ok=True)
        return self.export_dir / f"policy_j_shadow_{date_str}.jsonl"

    def append_snapshot(self, snapshot: PolicyJShadowSnapshot, date_str: str) -> bool:
        """
        Append a single canonical PolicyJShadowSnapshot JSON line to the target session file.
        Returns True on success, False on failure (with zero exception propagation).
        """
        from datetime import datetime
        now_str = datetime.now().isoformat()
        try:
            target_file = self.resolve_target_file(date_str)
            json_line = snapshot.to_json() + "\n"
            
            with open(target_file, "a", encoding="utf-8") as f:
                f.write(json_line)
                f.flush()
                
            self.records_written += 1
            self.consecutive_failures = 0
            self.last_success_time = now_str
            return True
        except Exception as e:
            self.write_error_count += 1
            self.consecutive_failures += 1
            self.last_error_time = now_str
            logger.error(
                f"[POLICY_J_TELEMETRY_WRITE_FAILED] Failed to append telemetry snapshot (id={snapshot.snapshot_id}): {e}",
                exc_info=True
            )
            return False

    def get_health_status(self) -> dict:
        """Return operational health status dictionary."""
        status = "HEALTHY"
        if self.consecutive_failures >= 5:
            status = "FAILED"
        elif self.consecutive_failures >= 1:
            status = "DEGRADED"

        return {
            "status": status,
            "records_written": self.records_written,
            "total_failures": self.write_error_count,
            "consecutive_failures": self.consecutive_failures,
            "last_success_time": self.last_success_time,
            "last_error_time": self.last_error_time,
        }
