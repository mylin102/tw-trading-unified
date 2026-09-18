"""Evidence capture and conservative attribution for broker divergence.

This module never changes broker or local trading state.  It turns one
reconciliation observation into a hash-bound diagnostic report and freezes
the caller's decision path until a human explicitly clears the hold.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable


ATTRIBUTIONS = frozenset({
    "PREMISE_VIOLATION_RELEASE",
    "UNRECORDED_FILL",
    "STALE_INTERNAL_FLAG",
    "SETTLEMENT_CLOSE",
    "LOST_ACK",
    "ENTRY_WITHOUT_PREMISE",
    "UNCLASSIFIED",
})


def _hash(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _rows(value: Iterable[Any] | None) -> list[dict[str, Any]]:
    return [dict(row) for row in (value or ()) if isinstance(row, dict)]


def _text(value: Any) -> str:
    return str(value or "").strip().upper()


def _event_action(event: dict[str, Any]) -> str:
    return _text(event.get("action") or event.get("intent")
                or event.get("intent_action") or event.get("side"))


def _event_code(event: dict[str, Any]) -> str:
    return str(event.get("code") or event.get("instrument_code")
               or event.get("symbol") or "").strip()


def _event_window(events: Iterable[Any] | None) -> list[dict[str, Any]]:
    return _rows(events)


def capture_evidence(*, broker_legs, local_legs, canonical_state=None,
                     internal_flags=None, ledger_tail=None,
                     event_window=None, broker_trades=None,
                     captured_at=None, source=None) -> dict[str, Any]:
    """Capture all supplied inputs once and hash each evidence section."""
    captured_at = time.time() if captured_at is None else captured_at
    sections = {
        "broker_legs": _rows(broker_legs),
        "local_legs": _rows(local_legs),
        "canonical_state": canonical_state if isinstance(canonical_state, dict) else {},
        "internal_flags": internal_flags if isinstance(internal_flags, dict) else {},
        "ledger_tail": _rows(ledger_tail),
        "event_window": _event_window(event_window),
        "broker_trades": _rows(broker_trades),
    }
    section_hashes = {name: _hash(value) for name, value in sections.items()}
    identity = {"source": source or "broker_reconciliation",
                "sections": sections}
    return {
        "captured_at": captured_at,
        "source": source or "broker_reconciliation",
        "sections": sections,
        "section_hashes": section_hashes,
        "evidence_hash": _hash(identity),
    }


def attribute_divergence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Classify only explicitly evidenced patterns; never guess."""
    sections = evidence.get("sections", {})
    broker = sections.get("broker_legs", [])
    local = sections.get("local_legs", [])
    ledger = sections.get("ledger_tail", [])
    events = sections.get("event_window", [])
    trades = sections.get("broker_trades", [])
    broker_codes = {str(row.get("code") or row.get("instrument_code")) for row in broker}
    local_codes = {str(row.get("code") or row.get("instrument_code")) for row in local}

    for event in events:
        action = _event_action(event)
        code = _event_code(event)
        if action == "RELEASE" and code and code not in broker_codes:
            return {"code": "PREMISE_VIOLATION_RELEASE",
                    "conclusion": "[inferred] RELEASE premise is absent at broker",
                    "basis": {"event": event}}
    for event in events:
        action = _event_action(event)
        code = _event_code(event)
        if action == "ENTRY" and code and code in broker_codes:
            return {"code": "ENTRY_WITHOUT_PREMISE",
                    "conclusion": "[inferred] ENTRY was observed while broker already held the leg",
                    "basis": {"event": event}}

    ledger_keys = {_fill_key(row) for row in ledger}
    for trade in trades:
        for fill in _rows(trade.get("deals", [])) or [trade]:
            if _fill_key(fill) not in ledger_keys and _fill_key(fill) != ("", "", ""):
                return {"code": "UNRECORDED_FILL",
                        "conclusion": "[inferred] broker fill has no matching ledger row",
                        "basis": {"fill": fill}}

    flags = sections.get("internal_flags", {})
    if flags.get("canonical_matches") is False or flags.get("stale") is True:
        return {"code": "STALE_INTERNAL_FLAG",
                "conclusion": "[inferred] internal flag is stale or disagrees with canonical state",
                "basis": {"internal_flags": flags}}

    for event in events:
        marker = _text(event.get("reason") or event.get("event") or event.get("status"))
        if "SETTLEMENT" in marker or "EXPIR" in marker or "DELIVER" in marker:
            return {"code": "SETTLEMENT_CLOSE",
                    "conclusion": "[inferred] settlement/expiry marker explains broker flattening",
                    "basis": {"event": event}}

    has_submit = any(_text(event.get("event") or event.get("status")) in
                     {"SUBMITTED", "ORDER_SUBMITTED", "SENT"} for event in events)
    has_ack = any(_text(event.get("event") or event.get("status")) in
                  {"ACK", "ORDER_ACK", "FILLED", "ORDER_FILLED"} for event in events)
    if has_submit and not has_ack and broker_codes:
        return {"code": "LOST_ACK",
                "conclusion": "[inferred] broker holding exists after submission without acknowledgement",
                "basis": {"event_window": events}}

    return {"code": "UNCLASSIFIED",
            "conclusion": "[observed] evidence does not satisfy an enumerated attribution",
            "basis": {"broker_legs": broker, "local_legs": local}}


def _fill_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("trade_id") or row.get("broker_trade_id") or ""),
            str(row.get("deal_id") or row.get("fill_id") or ""),
            str(row.get("instrument_code") or row.get("code") or ""))


def write_report(report_dir: str | Path | None, *, evidence, attribution,
                 reason: str, frozen: bool = True) -> tuple[str, bool]:
    """Persist one deduplicated report and return (path, duplicate)."""
    if report_dir is None:
        return "", False
    directory = Path(report_dir)
    directory.mkdir(parents=True, exist_ok=True)
    fingerprint = _hash({"evidence_hash": evidence["evidence_hash"],
                         "reason": reason,
                         "attribution": attribution["code"]})
    path = directory / f"divergence-{fingerprint}.json"
    if path.exists():
        return str(path), True
    report = {
        "event": "BROKER_DIVERGENCE_DIAGNOSTIC",
        "fingerprint": fingerprint,
        "captured_at": evidence["captured_at"],
        "evidence": evidence,
        "facts": [
            "[observed] broker/local divergence detected",
            f"[observed] freeze_send_orders={bool(frozen)}",
        ],
        "attribution": attribution,
        "disposition": "FROZEN_PENDING_HUMAN_CONFIRMATION",
        "recommendation": "[inferred] investigate using the evidence before any manual release",
        "reason": reason,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return str(path), False
