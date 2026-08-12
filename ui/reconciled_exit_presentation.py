"""Dashboard-only presentation for RECONCILED_EXIT_ONLY.

The restricted recovery mode must never reuse paper/local MTS state. Its
UPL is presentable only from the broker-attested capability and a
current, hash-bound dual-leg Shioaji BBO payload.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

EXIT_ONLY_BBO_TTL_MS = 15_000


def _hash_of(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   allow_nan=False).encode()).hexdigest()


def latest_bbo_evidence_from_events(events_path: Any) -> Optional[dict]:
    """Newest-first scan for the latest decision-bound dual BBO evidence
    (bbo_hash + bbo_payload) in the shared MTS event ledger.  Missing
    file or no payload-carrying event => None (display N/A)."""
    import os
    if not events_path or not os.path.exists(str(events_path)):
        return None
    try:
        with open(str(events_path), encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
    except Exception:
        return None
    for _line in reversed(lines):
        try:
            _ev = json.loads(_line)
        except Exception:
            continue
        if _ev.get("bbo_hash") and _ev.get("bbo_payload"):
            return {"bbo_hash": _ev["bbo_hash"],
                    "bbo_payload": _ev["bbo_payload"]}
    return None


def exit_only_upl_presentation(context: Any, evidence: Any, *,
                               now_ms: int, point_value: float = 10.0,
                               legacy_state: Any = None) -> Optional[dict]:
    """Present the RECONCILED_EXIT_ONLY UPL from the broker-attested
    capability legs and the hash-bound dual Shioaji BBO payload.

    Returns:
      {"kind": "COMPUTED", near/far pnl, total_pnl,
       source: "broker_attested_dual_bbo"} on valid evidence;
      {"kind": "NA", reason, total_pnl: None} on missing/stale/
      identity/hash/symbol mismatch;
      None for any non-EXIT_ONLY context (LIVE/PAPER untouched).
    `legacy_state` is accepted for signature parity but NEVER read —
    paper/local ledger values are never a fallback.
    """
    if not isinstance(context, dict):
        return None
    if context.get("effective_mode") != "reconciled_exit_only":
        return None
    cap = context.get("exit_only_capability")
    if not isinstance(cap, dict):
        return {"kind": "NA", "reason": "EXIT_ONLY_CAPABILITY_MISSING",
                "total_pnl": None}
    legs = cap.get("legs") or []
    if len(legs) != 2:
        return {"kind": "NA", "reason": "EXIT_ONLY_CAPABILITY_MISSING",
                "total_pnl": None}
    if not isinstance(evidence, dict):
        return {"kind": "NA", "reason": "EXIT_ONLY_BBO_MISSING",
                "total_pnl": None}
    payload = evidence.get("bbo_payload")
    bbo_hash = evidence.get("bbo_hash")
    if not isinstance(payload, dict) or not bbo_hash:
        return {"kind": "NA", "reason": "EXIT_ONLY_BBO_MISSING",
                "total_pnl": None}
    try:
        if _hash_of(payload) != bbo_hash:
            return {"kind": "NA",
                    "reason": "EXIT_ONLY_BBO_HASH_MISMATCH",
                    "total_pnl": None}
    except Exception:
        return {"kind": "NA", "reason": "EXIT_ONLY_BBO_HASH_MISMATCH",
                "total_pnl": None}
    for _k in ("reconciliation_id", "snapshot_hash", "config_hash",
               "release_sha", "session_id"):
        if payload.get(_k) != cap.get(_k):
            return {"kind": "NA",
                    "reason": "EXIT_ONLY_IDENTITY_MISMATCH",
                    "total_pnl": None}
    near = payload.get("near")
    far = payload.get("far")
    if not isinstance(near, dict) or not isinstance(far, dict):
        return {"kind": "NA", "reason": "EXIT_ONLY_BBO_MISSING",
                "total_pnl": None}
    if near.get("symbol") != legs[0].get("symbol") \
            or far.get("symbol") != legs[1].get("symbol"):
        return {"kind": "NA", "reason": "EXIT_ONLY_SYMBOL_MISMATCH",
                "total_pnl": None}
    # [Dashboard] the event JSONL is untrusted — independently validate
    # the BBO source and the quote shape (finite positive bid <= ask).
    if near.get("source") != "shioaji_bidask" \
            or far.get("source") != "shioaji_bidask":
        return {"kind": "NA", "reason": "EXIT_ONLY_SOURCE_MISMATCH",
                "total_pnl": None}
    import math as _math
    for _rec in (near, far):
        _bid = _rec.get("bid")
        _ask = _rec.get("ask")
        if (not isinstance(_bid, (int, float)) or isinstance(_bid, bool)
                or not _math.isfinite(float(_bid)) or float(_bid) <= 0
                or not isinstance(_ask, (int, float))
                or isinstance(_ask, bool)
                or not _math.isfinite(float(_ask)) or float(_ask) <= 0):
            return {"kind": "NA", "reason": "EXIT_ONLY_BBO_INVALID",
                    "total_pnl": None}
        if float(_bid) > float(_ask):
            return {"kind": "NA", "reason": "EXIT_ONLY_BBO_INVALID",
                    "total_pnl": None}
    _now = int(now_ms)
    for _rec in (near, far):
        _ts = _rec.get("exchange_ts")
        if not isinstance(_ts, int) or _ts <= 0:
            return {"kind": "NA", "reason": "EXIT_ONLY_BBO_STALE",
                    "total_pnl": None}
        if _ts > _now + 1_000:
            return {"kind": "NA", "reason": "EXIT_ONLY_BBO_STALE",
                    "total_pnl": None}
        if _now - _ts > EXIT_ONLY_BBO_TTL_MS:
            return {"kind": "NA", "reason": "EXIT_ONLY_BBO_STALE",
                    "total_pnl": None}
    _nq = float(legs[0].get("remaining_qty", 1) or 1)
    _fq = float(legs[1].get("remaining_qty", 1) or 1)
    _na = float(legs[0].get("avg_cost", 0.0) or 0.0)
    _fa = float(legs[1].get("avg_cost", 0.0) or 0.0)
    _near_mark = float(near.get("ask", near.get("bid", 0.0)))
    _far_mark = float(far.get("bid", far.get("ask", 0.0)))
    if legs[0].get("side") == "sell":
        _near_pnl = (_na - _near_mark) * _nq * point_value
    else:
        _near_pnl = (_near_mark - _na) * _nq * point_value
    if legs[1].get("side") == "sell":
        _far_pnl = (_fa - _far_mark) * _fq * point_value
    else:
        _far_pnl = (_far_mark - _fa) * _fq * point_value
    return {"kind": "COMPUTED",
            "near": {"pnl": round(_near_pnl, 6)},
            "far": {"pnl": round(_far_pnl, 6)},
            "total_pnl": round(_near_pnl + _far_pnl, 6),
            "source": "broker_attested_dual_bbo"}


def exit_only_upl_metrics(context: Any, events_path: Any, *,
                          now_ms: int, point_value: float = 10.0,
                          legacy_state: Any = None) -> Optional[dict]:
    """[Dashboard] exit-only UPL for the MTS panels: scan the event
    ledger for the latest hash-bound dual BBO evidence and present it
    against the capability.  None for non-EXIT_ONLY contexts; NA dict
    with the typed reason otherwise."""
    if not isinstance(context, dict) \
            or context.get("effective_mode") != "reconciled_exit_only":
        return None
    evidence = latest_bbo_evidence_from_events(events_path)
    return exit_only_upl_presentation(
        context, evidence, now_ms=now_ms, point_value=point_value,
        legacy_state=legacy_state)
