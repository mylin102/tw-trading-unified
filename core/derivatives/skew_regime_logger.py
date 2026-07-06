"""
Skew Regime JSONL Logger — persist every VolState decision for replay/analysis.

One JSONL file per trading day, written to:
  logs/skew_regime/YYYYMMDD.jsonl

Each line is a complete SkewRegime dict plus a write timestamp.
Used by:
  - Dashboard for live display
  - Post-session analysis for regime stability validation
  - Backtest replay of vol state decisions
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
from threading import Lock

logger = logging.getLogger(__name__)


class SkewRegimeLogger:
    """JSONL logger for skew regime decisions.

    Thread-safe: uses lock for concurrent writes from monitor main loop.

    Usage:
        logger = SkewRegimeLogger(base_dir="logs/skew_regime")
        logger.write(skew_regime_dict)
    """

    def __init__(self, base_dir: str = "logs/skew_regime"):
        self._base_dir = Path(base_dir)
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._current_file: Optional[Path] = None
        self._current_date: str = ""

    def write(self, regime_dict: dict) -> None:
        """Write one SkewRegime decision to today's JSONL file.

        Thread-safe. Creates new file per trading day.
        """
        if regime_dict is None:
            return

        now = datetime.datetime.utcnow()
        date_str = now.strftime("%Y%m%d")

        with self._lock:
            # Rotate file if trading day changed
            if date_str != self._current_date:
                self._current_date = date_str
                self._current_file = self._base_dir / f"{date_str}.jsonl"

            # Add write timestamp
            record = dict(regime_dict)
            record["_written_at"] = now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

            try:
                with open(str(self._current_file), "a") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as e:
                logger.warning("[SkewRegimeLogger] write error: %s", e)

    def read_today(self) -> list[dict]:
        """Read today's JSONL file for dashboard display."""
        date_str = datetime.datetime.utcnow().strftime("%Y%m%d")
        path = self._base_dir / f"{date_str}.jsonl"
        if not path.exists():
            return []
        try:
            records = []
            with open(str(path)) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            return records
        except Exception as e:
            logger.warning("[SkewRegimeLogger] read error: %s", e)
            return []

    def read_date(self, date_str: str) -> list[dict]:
        """Read a specific date's JSONL file."""
        path = self._base_dir / f"{date_str}.jsonl"
        if not path.exists():
            return []
        try:
            records = []
            with open(str(path)) as f:
                for line in f:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
            return records
        except Exception as e:
            logger.warning("[SkewRegimeLogger] read error: %s", e)
            return []
