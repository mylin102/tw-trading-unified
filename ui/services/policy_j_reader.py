# 2026-07-26 Gemini CLI: Wave J1.5-C Read-Only Policy J Telemetry Reader
import json
import logging
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


class PolicyJTelemetryReader:
    """
    Read-Only JSONL Telemetry Reader for Policy J Shadow Mode UI.
    100% read-only — parses session JSONL files with major-version validation,
    partial line tolerance, deduplication, and sorting.
    """

    def __init__(self, export_dir: Optional[Path] = None):
        if export_dir is None:
            project_root = Path(__file__).parent.parent.parent
            export_dir = project_root / "exports" / "telemetry" / "policy_j"
        self.export_dir = Path(export_dir)

    def list_available_session_dates(self) -> List[str]:
        """List all available session dates from telemetry JSONL filenames."""
        if not self.export_dir.exists():
            return []
        files = list(self.export_dir.glob("policy_j_shadow_*.jsonl"))
        dates = []
        for f in files:
            # Format: policy_j_shadow_YYYYMMDD.jsonl
            parts = f.stem.split("policy_j_shadow_")
            if len(parts) > 1 and parts[1]:
                dates.append(parts[1])
        return sorted(list(set(dates)), reverse=True)

    def load_snapshots(
        self, date_str: str, trade_id: Optional[str] = None
    ) -> List[dict[str, Any]]:
        """
        Load and parse snapshots from policy_j_shadow_YYYYMMDD.jsonl.
        Tolerates partial/corrupted final lines and rejects incompatible major schema versions (!= 1).
        """
        target_file = self.export_dir / f"policy_j_shadow_{date_str}.jsonl"
        if not target_file.exists():
            return []

        snapshots = []
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        record = json.loads(line_str)
                        # Major version check: must be 1.x
                        schema_ver = str(record.get("schema_version", "1.0"))
                        if not schema_ver.startswith("1."):
                            logger.warning(f"Skipping incompatible schema version '{schema_ver}'")
                            continue

                        if trade_id and record.get("trade_id") != trade_id:
                            continue

                        snapshots.append(record)
                    except Exception:
                        # Gracefully ignore partial/corrupted final line
                        continue
        except Exception as e:
            logger.error(f"Failed to read telemetry file {target_file}: {e}")
            return []

        # Sort snapshots by sequence_no
        snapshots.sort(key=lambda s: s.get("sequence_no", 0))
        return snapshots
