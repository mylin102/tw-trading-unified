"""Fail-closed broker-query consensus and state convergence.

Broker queries are authoritative, but one empty query is only an observation,
not proof of flatness.  This module is read-only with respect to the broker:
it only atomically writes local state/audit after a configurable consensus.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _fingerprint(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _positions(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = state.get("positions", state.get("legs", []))
    return list(rows or []) if isinstance(rows, list) else []


def _capture_seconds(value: Any) -> float:
    number = float(value)
    return number / 1000.0 if number >= 1e12 else number


@dataclass(frozen=True)
class ConsensusResult:
    state: dict[str, Any]
    consensus: bool
    pending_observations: int
    reason: str


class BrokerTruthConsensus:
    """Accumulate identical broker captures before changing local state."""

    def __init__(self, state_path: str | Path, audit_path: str | Path,
                 *, required_observations: int = 2,
                 min_interval_seconds: float = 60.0):
        if type(required_observations) is not int or required_observations < 2:
            raise ValueError("required_observations must be >= 2")
        if min_interval_seconds < 0:
            raise ValueError("min_interval_seconds must be >= 0")
        self.state_path = Path(state_path)
        self.audit_path = Path(audit_path)
        self.required_observations = required_observations
        self.min_interval_seconds = float(min_interval_seconds)
        self._pending_key: str | None = None
        self._pending: list[dict[str, Any]] = []
        self._pending_codes: set[str] = set()
        self._last_state: dict[str, Any] | None = None

    def exit_allowed(self, code: str) -> bool:
        """P0 guard: no exit for an affected leg while truth is unsettled."""
        code = str(code)
        if code in self._pending_codes:
            return False
        if self._last_state is not None:
            return code in {str(row.get("code")) for row in _positions(self._last_state)
                            if isinstance(row, dict)}
        return True

    def _audit(self, event: dict[str, Any]) -> None:
        prior = []
        if self.audit_path.exists():
            try:
                prior = json.loads(self.audit_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                prior = []
        if not isinstance(prior, list):
            prior = []
        _atomic_json(self.audit_path, prior + [event])

    def observe(self, capture: dict[str, Any], state: dict[str, Any], *,
                now: float | None = None,
                capture_path: str | None = None) -> ConsensusResult:
        now = time.time() if now is None else float(now)
        current = dict(state)
        source = capture.get("source")
        rows = capture.get("positions")
        if source != "live_broker" or capture.get("capture_error") or not isinstance(rows, list):
            self._pending_codes = set()
            return ConsensusResult(current, False, len(self._pending),
                                   "BROKER_EVIDENCE_UNAVAILABLE")
        if capture.get("open_orders") != []:
            return ConsensusResult(current, False, len(self._pending),
                                   "BROKER_WORKING_ORDER_HOLD")

        old_rows = _positions(current)
        new_key = _fingerprint(rows)
        old_key = _fingerprint(old_rows)
        if self._pending and self._pending_key != new_key:
            self._audit({"event": "BROKER_TRUTH_FLAP",
                         "captured_at": capture.get("captured_at"),
                         "canonical_input_hash": capture.get("canonical_input_hash"),
                         "reason": "observation changed before consensus"})
            self._pending.clear()
            self._pending_key = None
        if new_key == old_key:
            self._pending.clear()
            self._pending_key = None
            self._pending_codes.clear()
            self._last_state = current
            return ConsensusResult(current, True, 0, "STATE_ALREADY_MATCHES_BROKER")

        codes = {str(row.get("code")) for row in old_rows + rows
                 if isinstance(row, dict) and row.get("code")}
        if self._pending_key != new_key:
            self._pending_key = new_key
            self._pending = []
            self._pending_codes = codes

        previous = self._pending[-1] if self._pending else None
        if previous is not None:
            elapsed = (_capture_seconds(capture.get("captured_at", now))
                       - _capture_seconds(previous["captured_at"]))
            if elapsed < self.min_interval_seconds:
                return ConsensusResult(current, False, len(self._pending),
                                       "CONSENSUS_INTERVAL_NOT_MET")
        entry = {"canonical_input_hash": capture.get("canonical_input_hash"),
                 "captured_at": capture.get("captured_at"),
                 "capture_path": capture_path}
        self._pending.append(entry)
        if len(self._pending) < self.required_observations:
            return ConsensusResult(current, False, len(self._pending),
                                   "BROKER_TRUTH_PENDING_CONSENSUS")

        reconciled = dict(current)
        reconciled["positions"] = rows
        reconciled["status"] = "FLAT" if not rows else "ENTRY_RECONCILED"
        reconciled["reconciled_from"] = "broker_query_consensus"
        reconciled["capture_paths"] = [x["capture_path"] for x in self._pending]
        reconciled["capture_hashes"] = [x["canonical_input_hash"] for x in self._pending]
        reconciled["reconciled_at"] = capture.get("captured_at")
        event = {"event": "BROKER_TRUTH_CONSENSUS",
                 "reconciled_from": "broker_query_consensus",
                 "difference": {"before": old_rows, "after": rows},
                 "captures": list(self._pending)}
        _atomic_json(self.state_path, reconciled)
        self._audit(event)
        self._pending.clear()
        self._pending_key = None
        self._pending_codes.clear()
        self._last_state = reconciled
        return ConsensusResult(reconciled, True, self.required_observations,
                               "BROKER_TRUTH_CONSENSUS")
