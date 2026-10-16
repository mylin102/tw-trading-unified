"""Canonical holding reconciliation for startup and reconnect.

The broker snapshot is retained verbatim as evidence.  A matching snapshot
hydrates the ordinary strategy position; it never creates a second exit
authorization mode.  Any uncertainty remains fail-closed in a
``reconciliation_hold``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Sequence


class ReconciliationStatus(str, Enum):
    RECONCILED = "RECONCILED"
    HOLD = "HOLD"


@dataclass(frozen=True)
class HoldingReconciliation:
    status: ReconciliationStatus
    context_mode: str
    hydrated: bool
    entry_blocked: bool
    fail_closed: bool
    reason: str = ""
    raw_broker_legs: tuple[dict, ...] = ()
    raw_local_legs: tuple[dict, ...] = ()
    canonical_legs: tuple[dict, ...] = ()
    exit_only_capability: None = None

    @property
    def quarantine(self) -> bool:
        """Compatibility spelling for callers of the removed old mode."""
        return self.status is ReconciliationStatus.HOLD


def _raw(legs: Sequence[dict] | None) -> tuple[dict, ...]:
    return tuple(dict(leg) for leg in legs or () if isinstance(leg, dict))


def _append_jsonl(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _mismatch_reason(broker: tuple[dict, ...], local: tuple[dict, ...],
                     tick_size: float) -> str:
    if len(broker) != len(local):
        return "LEG_COUNT_MISMATCH"
    for left, right in zip(broker, local):
        if (left.get("code") != right.get("code")
                or str(left.get("side", "")).upper()
                != str(right.get("side", "")).upper()
                or left.get("qty") != right.get("qty")):
            return "LEG_IDENTITY_MISMATCH"
        try:
            if abs(float(left["avg_cost"]) - float(right["avg_cost"])) > float(tick_size) / 2:
                return "AVG_COST_MISMATCH"
        except (KeyError, TypeError, ValueError):
            return "AVG_COST_UNREADABLE"
    return ""


def reconcile_holding(
    broker_legs: Sequence[dict] | None,
    local_legs: Sequence[dict] | None,
    *,
    tick_size: float,
    broker_query_error: str | None = None,
    state_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    alert_path: str | Path | None = None,
    broker_capture: dict[str, Any] | None = None,
    local_source_path: str | Path | None = None,
) -> HoldingReconciliation:
    """Compare broker and local canonical legs with an explicit tick rule.

    Average cost is equal when the absolute difference is at most half a
    tick.  This is deliberately stricter than the legacy broad ``0.5`` rule
    and applies only after code, side and quantity match.
    """
    broker_raw = _raw(broker_legs)
    local_raw = _raw(local_legs)
    capture = broker_capture or {}
    unavailable = (broker_query_error or broker_legs is None
                   or capture.get("source") == "unavailable"
                   or capture.get("capture_error"))
    if unavailable:
        return HoldingReconciliation(
            ReconciliationStatus.HOLD, "reconciliation_hold", False, True,
            True, broker_query_error or "BROKER_POSITIONS_UNAVAILABLE",
            broker_raw, local_raw)
    reason = _mismatch_reason(broker_raw, local_raw, tick_size)
    if reason:
        event = {
            "event": "BROKER_STATE_DIVERGENCE",
            "source": capture.get("source") or "broker_query",
            "source_path": capture.get("source_path"),
            "source_mtime": (Path(capture["source_path"]).stat().st_mtime
                              if capture.get("source_path") and Path(capture["source_path"]).exists()
                              else None),
            "canonical_input_hash": capture.get("canonical_input_hash"),
            "broker_legs": [dict(x) for x in broker_raw],
            "local_legs": [dict(x) for x in local_raw],
            "difference": reason,
            "disposition": "converged_to_broker",
            "audit_path": str(audit_path) if audit_path else None,
        }
        if state_path:
            state_file = Path(state_path)
            if state_file.exists():
                backup = state_file.with_name(
                    f"{state_file.name}.bak.{int(time.time() * 1000)}")
                shutil.copy2(state_file, backup)
                event["state_backup_path"] = str(backup)
            new_state = {}
            if state_file.exists():
                try:
                    new_state = json.loads(state_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    new_state = {}
            new_state["positions"] = [dict(x) for x in broker_raw]
            new_state["status"] = "FLAT" if not broker_raw else "ENTRY_RECONCILED"
            new_state["reconciled_from"] = "broker_query_consensus"
            new_state["canonical_input_hash"] = capture.get("canonical_input_hash")
            _atomic_state(state_file, new_state)
        if audit_path:
            _append_jsonl(Path(audit_path), event)
        if alert_path:
            _append_jsonl(Path(alert_path), {
                "event": "BROKER_STATE_DIVERGENCE",
                "severity": "WARNING",
                "difference": reason,
                "disposition": "converged_to_broker",
                "audit_path": str(audit_path) if audit_path else None,
            })
        return HoldingReconciliation(
            ReconciliationStatus.RECONCILED, "live_ready", True,
            bool(broker_raw), False, reason, broker_raw, local_raw,
            broker_raw, None)
    return HoldingReconciliation(
        ReconciliationStatus.RECONCILED, "live_ready", True,
        bool(broker_raw), False, "", broker_raw, local_raw, broker_raw, None)
