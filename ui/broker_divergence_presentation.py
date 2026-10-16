"""Read-only dashboard projection for broker/local divergence audit JSONL."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _seconds(value: Any) -> float | None:
    try:
        number = float(value)
        return number / 1000.0 if number >= 1e12 else number
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return None


def _leg(row: Any) -> dict[str, Any]:
    row = row if isinstance(row, dict) else {}
    return {key: row.get(key, "N/A") if row.get(key) not in (None, "") else "N/A"
            for key in ("code", "side", "qty", "avg_cost")}


def _row(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": event.get("captured_at", "N/A") or "N/A",
        "source": event.get("source", "N/A") or "N/A",
        "broker_legs": [_leg(x) for x in event.get("broker_legs", [])],
        "local_legs": [_leg(x) for x in event.get("local_legs", [])],
        "difference": event.get("difference", "N/A") or "N/A",
        "disposition": event.get("disposition", "N/A") or "N/A",
        "audit_path": event.get("audit_path", "N/A") or "N/A",
    }


def divergence_panel(audit_path: str | Path, *, max_events: int = 10,
                     max_age_seconds: float = 600.0,
                     now: float | None = None) -> dict[str, Any]:
    """Return dashboard data without writing reconcile/state files."""
    path = Path(audit_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        return {"status": "ERROR", "events": [], "error": str(exc),
                "latest_mismatch_pending": False}
    events = []
    try:
        for line in lines:
            if line.strip():
                event = json.loads(line)
                if event.get("event") == "BROKER_STATE_DIVERGENCE":
                    events.append(event)
    except (TypeError, ValueError) as exc:
        return {"status": "ERROR", "events": [], "error": str(exc),
                "latest_mismatch_pending": False}
    if not events:
        return {"status": "N/A", "events": [], "error": "N/A",
                "latest_mismatch_pending": False}
    now = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    latest_ts = _seconds(events[-1].get("captured_at"))
    if latest_ts is None or now - latest_ts > max_age_seconds:
        return {"status": "STALE", "events": [], "error": "STALE",
                "latest_mismatch_pending": False}
    recent = events[-max(1, int(max_events)):]
    return {
        "status": "OK",
        "events": [_row(event) for event in recent],
        "error": "N/A",
        "latest_mismatch_pending": events[-1].get("disposition") != "converged_to_broker",
    }
