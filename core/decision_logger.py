"""
Decision Logger — append-only audit trail for every adaptive decision.

Usage:
    from core.decision_logger import DecisionLogger
    DecisionLogger.log(
        type="post_session",
        session="day",
        action="tighten_entry",
        detail="confirm_bars 7→10",
        author="system",
        risk_level="low",
    )

Output: logs/decisions.csv (append-only, never deleted or modified)
"""
from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
import csv
import threading

_DECISIONS_PATH = Path(__file__).resolve().parent.parent / "logs" / "decisions.csv"
_HEADERS = [
    "timestamp", "type", "session", "action", "detail", "author", "risk_level", "status",
]
_lock = threading.Lock()


@dataclass
class Decision:
    timestamp: str
    type: str           # post_session, intra_session, circuit_breaker, config_change, audit
    session: str        # day, night, all
    action: str         # tighten_entry, switch_strategy, halt, reduce_size, cooldown, param_edit
    detail: str         # human-readable description
    author: str         # system, user, ceo_review
    risk_level: str     # low, medium, high
    status: str = "active"  # active, reverted, superseded


class DecisionLogger:
    """Append-only CSV logger for adaptive trading decisions."""

    @staticmethod
    def _ensure_file(path: Path = _DECISIONS_PATH) -> Path:
        """Create directory and file with headers if missing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_HEADERS)
                writer.writeheader()
        return path

    @classmethod
    def log(
        cls,
        type: str,
        session: str,
        action: str,
        detail: str,
        author: str = "system",
        risk_level: str = "low",
        status: str = "active",
        path: Path | None = None,
    ) -> Decision:
        """
        Log a decision to the append-only CSV.
        Thread-safe via threading.Lock.

        Returns:
            The Decision object that was logged.
        """
        out_path = path or _DECISIONS_PATH
        cls._ensure_file(out_path)

        decision = Decision(
            timestamp=datetime.now().isoformat(timespec="seconds"),
            type=type,
            session=session,
            action=action,
            detail=detail,
            author=author,
            risk_level=risk_level,
            status=status,
        )

        with _lock:
            with open(out_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_HEADERS)
                writer.writerow(asdict(decision))

        return decision

    @classmethod
    def read(cls, limit: int = 50, path: Path | None = None) -> list[Decision]:
        """Read the most recent N decisions (newest first)."""
        out_path = path or _DECISIONS_PATH
        if not out_path.exists():
            return []
        rows = []
        with _lock:
            with open(out_path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
        # Newest first
        rows.reverse()
        return [Decision(**r) for r in rows[:limit]]

    @classmethod
    def read_by_session(
        cls, session: str, limit: int = 20, path: Path | None = None
    ) -> list[Decision]:
        """Read decisions for a specific session type."""
        all_decisions = cls.read(limit=9999, path=path)
        return [d for d in all_decisions if d.session in (session, "all")][:limit]
