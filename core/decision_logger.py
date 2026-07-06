"""
Decision Logger — append-only audit trail for every adaptive decision and trade outcome.

Features:
- Log system decisions (tighten entry, switch strategy, etc.)
- Log trade outcomes with feature attribution (why it won/lost)
"""
from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, asdict
import csv
import json
import threading

_DECISIONS_PATH = Path(__file__).resolve().parent.parent / "logs" / "decisions.csv"
_TRADE_ATTRIBUTION_PATH = Path(__file__).resolve().parent.parent / "logs" / "trade_attribution.csv"

_DECISION_HEADERS = [
    "timestamp", "type", "session", "action", "detail", "author", "risk_level", "status",
]
# Backwards-compatible alias expected by older tests
_HEADERS = _DECISION_HEADERS
_ATTRIBUTION_HEADERS = [
    "timestamp", "trade_id", "strategy", "regime", "features", "outcome", "attribution"
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
    """Append-only CSV logger for adaptive trading decisions and trade outcomes."""

    @staticmethod
    def _ensure_file(path: Path, headers: list[str]) -> Path:
        """Create directory and file with headers if missing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
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
        path: str | Path | None = None,
    ) -> Decision:
        """Log a system decision. If `path` is provided, use it instead of the default file.
        This preserves backwards compatibility with older tests that pass a path kwarg.
        """
        target = Path(path) if path is not None else _DECISIONS_PATH
        cls._ensure_file(target, _DECISION_HEADERS)

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
            with open(target, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_DECISION_HEADERS)
                writer.writerow(asdict(decision))

        return decision

    @classmethod
    def read(cls, path: str | Path):
        """Read decisions from a given CSV path. Returns newest-first list of Decision dataclasses."""
        p = Path(path)
        if not p.exists():
            return []
        rows = []
        with _lock:
            with open(p, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
        rows.reverse()
        return [Decision(**r) for r in rows]

    @classmethod
    def read_by_session(cls, session: str, path: str | Path):
        all_rows = cls.read(path=path)
        return [r for r in all_rows if r.session == session]

    @classmethod
    def log_trade_outcome(
        cls,
        trade_id: str,
        strategy: str,
        regime: str,
        features: dict,
        outcome: dict,
        attribution: dict | None = None
    ):
        """
        Log a trade outcome with feature attribution.
        
        Args:
            trade_id: Unique identifier for the trade
            strategy: Strategy name
            regime: Market regime at entry
            features: Context at entry (JSON-serializable dict)
            outcome: PnL, exit reason, etc. (JSON-serializable dict)
            attribution: Analysis of why it won/lost (optional)
        """
        cls._ensure_file(_TRADE_ATTRIBUTION_PATH, _ATTRIBUTION_HEADERS)
        
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "trade_id": trade_id,
            "strategy": strategy,
            "regime": regime,
            "features": json.dumps(features),
            "outcome": json.dumps(outcome),
            "attribution": json.dumps(attribution or {})
        }
        
        with _lock:
            with open(_TRADE_ATTRIBUTION_PATH, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=_ATTRIBUTION_HEADERS)
                writer.writerow(row)

    @classmethod
    def read_decisions(cls, limit: int = 50) -> list[Decision]:
        """Read newest decisions."""
        if not _DECISIONS_PATH.exists(): return []
        rows = []
        with _lock:
            with open(_DECISIONS_PATH, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader: rows.append(row)
        rows.reverse()
        return [Decision(**r) for r in rows[:limit]]
