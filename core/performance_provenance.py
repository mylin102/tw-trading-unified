"""MTS Paper/Live performance provenance — canonical presentation contract.

Every MTS performance metric (UPL, realized PnL, closed-loop count / win
rate / profit factor) must be SCOPED by:
  - mode (paper / live)
  - run_id
  - config_hash
  - session/source provenance

Rules (fail-closed presentation — NEVER 0 or merged PnL on uncertainty):
  - the Live view may use ONLY live broker-reconciled execution evidence;
    the Paper view ONLY paper evidence
  - legacy (no per-record provenance) / mixed / unknown evidence renders
    N/A with an explicit source-mismatch reason
  - a live verified-flat UPL renders 0 ONLY together with a fresh
    snapshot timestamp; missing/stale evidence renders N/A
  - Go-Live Preconditions stay visibly separate (research/observation),
    never presented as performance

The classifier is dashboard-side and pure (no streamlit import): the
ledger records may or may not carry explicit mode/run_id/config_hash —
records without them are legacy (attributable to a paper runtime, never
to a live runtime).
"""

import json
import os
import time

# freshness window for a "verified now" snapshot (same as the gate SLO)
SNAPSHOT_MAX_AGE_S = 600.0

_EVIDENCE_FIELDS = ("mode", "run_id", "config_hash")
# the ONLY accepted live source marker (documented canonical equivalent)
LIVE_SOURCE = "live_broker_reconciled"
_SESSION_KEYS = ("session_id", "generation", "session_generation")


def _iter_records(path):
    if not path or not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(rec, dict) and rec:
                yield rec


def classify_mts_evidence(fills_path, events_path) -> dict:
    """Scan the MTS ledger and classify the execution evidence.

    Returns:
      evidence_mode: "live" | "paper" | "legacy" | "mixed" | "missing"
      run_ids / config_hashes / session_ids / sources: distinct values
      record_count: scanned records
      reason: None or an explicit source-attribution explanation

    LIVE is STRICT (fail-closed): evidence is classified "live" ONLY when
    EVERY record carries mode="live" AND exactly one non-empty run_id AND
    exactly one non-empty config_hash AND exactly one non-empty
    session/generation AND every source == "live_broker_reconciled".
    Any missing/mixed/unknown value degrades to "mixed" (N/A downstream).
    """
    modes, run_ids, hashes, sessions, sources = set(), set(), set(), set(), set()
    n = 0
    for path in (fills_path, events_path):
        for rec in _iter_records(path):
            n += 1
            m = rec.get("mode")
            if isinstance(m, str) and m:
                modes.add(m)
            r = rec.get("run_id")
            if isinstance(r, str) and r:
                run_ids.add(r)
            c = rec.get("config_hash")
            if isinstance(c, str) and c:
                hashes.add(c)
            s = next((rec.get(k) for k in _SESSION_KEYS
                      if isinstance(rec.get(k), str) and rec.get(k)), None)
            if s:
                sessions.add(s)
            src = rec.get("source")
            if isinstance(src, str) and src:
                sources.add(src)
    if n == 0:
        return {"evidence_mode": "missing", "record_count": 0,
                "run_ids": [], "config_hashes": [], "session_ids": [],
                "sources": [], "reason": None}
    if modes:
        known = modes & {"paper", "live"}
        if known and len(known) > 1:
            return {"evidence_mode": "mixed", "record_count": n,
                    "run_ids": sorted(run_ids),
                    "config_hashes": sorted(hashes),
                    "session_ids": sorted(sessions),
                    "sources": sorted(sources),
                    "reason": "ledger mixes paper and live execution "
                              "evidence — cannot attribute"}
        if known:
            mode = next(iter(known))
            unknown = modes - {"paper", "live"}
            if unknown:
                return {"evidence_mode": "mixed", "record_count": n,
                        "run_ids": sorted(run_ids),
                        "config_hashes": sorted(hashes),
                        "session_ids": sorted(sessions),
                        "sources": sorted(sources),
                        "reason": f"ledger carries unknown modes "
                                  f"{sorted(unknown)} — cannot attribute"}
            if mode == "live":
                # STRICT live: the full provenance must be present and
                # single-valued on EVERY record (audit correction — an
                # unproven live ledger must NEVER pass)
                missing = []
                if not run_ids:
                    missing.append("run_id")
                elif len(run_ids) > 1:
                    missing.append(f"multiple run_ids {sorted(run_ids)}")
                if not hashes:
                    missing.append("config_hash")
                elif len(hashes) > 1:
                    missing.append(f"multiple config_hashes "
                                   f"{sorted(hashes)}")
                if not sessions:
                    missing.append("session/generation")
                elif len(sessions) > 1:
                    missing.append(f"multiple sessions {sorted(sessions)}")
                if not sources:
                    missing.append("source")
                elif sources != {LIVE_SOURCE}:
                    missing.append(f"source {sorted(sources)} != "
                                   f"{LIVE_SOURCE}")
                if missing:
                    return {"evidence_mode": "mixed", "record_count": n,
                            "run_ids": sorted(run_ids),
                            "config_hashes": sorted(hashes),
                            "session_ids": sorted(sessions),
                            "sources": sorted(sources),
                            "reason": "live ledger incomplete/unproven: "
                                      + "; ".join(missing)}
                return {"evidence_mode": "live", "record_count": n,
                        "run_ids": sorted(run_ids),
                        "config_hashes": sorted(hashes),
                        "session_ids": sorted(sessions),
                        "sources": sorted(sources), "reason": None}
            # paper mode (explicit)
            return {"evidence_mode": mode, "record_count": n,
                    "run_ids": sorted(run_ids),
                    "config_hashes": sorted(hashes),
                    "session_ids": sorted(sessions),
                    "sources": sorted(sources), "reason": None}
        # only unknown mode strings
        return {"evidence_mode": "mixed", "record_count": n,
                "run_ids": sorted(run_ids),
                "config_hashes": sorted(hashes),
                "session_ids": sorted(sessions),
                "sources": sorted(sources),
                "reason": "ledger carries no recognizable paper/live mode "
                          "provenance — cannot attribute"}
    # no per-record mode: legacy evidence
    return {"evidence_mode": "legacy", "record_count": n,
            "run_ids": sorted(run_ids), "config_hashes": sorted(hashes),
            "session_ids": sorted(sessions), "sources": sorted(sources),
            "reason": "legacy ledger without per-record mode/run_id/"
                      "config_hash provenance"}


def scope_mts_performance(runtime_truth: dict, evidence: dict) -> dict:
    """The canonical presentation scope for MTS performance data.

    runtime_truth: the dashboard's summarize_execution_context() result
      (is_live_runtime / is_paper_runtime / profile_identity /
      config_hash / runtime_status)
    evidence: classify_mts_evidence() result

    Returns {"ok": bool, "reason": None|str, "mode": ..., "run_id": ...,
             "config_hash": ...} — ok=False means the metrics must render
    N/A with `reason` (NEVER 0, NEVER merged PnL).
    """
    rt = runtime_truth if isinstance(runtime_truth, dict) else {}
    ev = evidence if isinstance(evidence, dict) else {}
    em = ev.get("evidence_mode")
    if rt.get("is_live_runtime"):
        mode = "live"
        if em == "live":
            # STRICT live (audit correction): the classifier already
            # guarantees every record has mode=live + one run_id + one
            # config_hash + one session + source=live_broker_reconciled.
            # The scope additionally requires config_hash == the ACTIVE
            # sealed profile hash AND the session == the ACTIVE runtime
            # session/generation.
            if ev.get("config_hashes") != [rt.get("config_hash")]:
                return {"ok": False, "mode": mode, "reason":
                        "live view: ledger config_hash does not match the "
                        "active sealed profile — source mismatch",
                        "run_id": None, "config_hash": rt.get("config_hash")}
            if rt.get("session_id") and \
                    ev.get("session_ids") != [rt.get("session_id")]:
                return {"ok": False, "mode": mode, "reason":
                        "live view: ledger session/generation does not "
                        "match the active runtime session — source "
                        "mismatch", "run_id": None,
                        "config_hash": rt.get("config_hash")}
            run_id = ev["run_ids"][0] if len(ev.get("run_ids") or []) == 1 \
                else None
            return {"ok": True, "mode": mode, "reason": None,
                    "run_id": run_id, "config_hash": rt.get("config_hash")}
        if em in ("paper", "legacy"):
            return {"ok": False, "mode": mode, "reason":
                    f"live view with {em} execution evidence — source "
                    "mismatch (live view requires live broker-reconciled "
                    "evidence)", "run_id": None,
                    "config_hash": rt.get("config_hash")}
        return {"ok": False, "mode": mode, "reason":
                f"live view: {em or 'missing'} execution evidence — "
                "cannot attribute", "run_id": None,
                "config_hash": rt.get("config_hash")}
    if rt.get("is_paper_runtime"):
        mode = "paper"
        if em in ("paper", "legacy"):
            # legacy records ARE the paper evidence (paper compatibility)
            return {"ok": True, "mode": mode, "reason": None,
                    "run_id": None, "config_hash": rt.get("config_hash")}
        if em == "live":
            return {"ok": False, "mode": mode, "reason":
                    "paper view with live execution evidence — source "
                    "mismatch (paper view requires paper evidence)",
                    "run_id": None, "config_hash": rt.get("config_hash")}
        return {"ok": False, "mode": mode, "reason":
                f"paper view: {em or 'missing'} execution evidence — "
                "cannot attribute", "run_id": None,
                "config_hash": rt.get("config_hash")}
    return {"ok": False, "mode": "unknown", "reason":
            f"runtime not an authorized live/paper state "
            f"({rt.get('runtime_status')}) — fail-closed",
            "run_id": None, "config_hash": rt.get("config_hash")}


def broker_snapshot_live_upl(snapshot_path) -> tuple:
    """Live UPL from the CURRENT broker-reconciled preflight snapshot:
    per-code pnl (fresh snapshot only).  Missing / stale / malformed =>
    (None, reason) so the UI renders N/A — the live UPL is NEVER
    fabricated from the local state."""
    try:
        from pathlib import Path
        _p = Path(str(snapshot_path))
        if not _p.exists():
            return None, "no broker snapshot"
        _resp = json.loads(_p.read_text(encoding="utf-8"))
        _snap = _resp.get("snapshot") or {}
        _positions = _snap.get("positions") or []
        if not _positions:
            return None, "broker snapshot has no positions"
        _cap_raw = _resp.get("captured_at")
        _cap_ms = None
        if isinstance(_cap_raw, (int, float)) and not isinstance(_cap_raw, bool):
            _cap_ms = int(_cap_raw)
        elif isinstance(_cap_raw, str):
            try:
                from datetime import datetime
                _dt = datetime.fromisoformat(_cap_raw.replace("Z", "+00:00"))
                _cap_ms = int(_dt.timestamp() * 1000)
            except Exception:
                _cap_ms = None
        if _cap_ms is None or time.time() - _cap_ms / 1000.0 > SNAPSHOT_MAX_AGE_S:
            return None, "broker snapshot stale/missing timestamp"
        _upl = {}
        for _pos in _positions:
            _code = str(_pos.get("code") or "")
            _pnl = _pos.get("pnl")
            if _code and isinstance(_pnl, (int, float)):
                _upl[_code] = _pnl
        if not _upl:
            return None, "no pnl in broker positions"
        return _upl, None
    except Exception as exc:
        return None, str(exc)


def upl_presentation(scope: dict, is_flat: bool, snapshot_ts) -> dict:
    """Canonical UPL presentation.

    - scope not ok -> N/A + the scope reason
    - live verified-flat: 0 ONLY with a fresh snapshot timestamp
      (now - snapshot_ts <= SNAPSHOT_MAX_AGE_S); stale/missing timestamp
      -> N/A (cannot verify the flat claim NOW)
    - paper flat -> 0 (paper evidence is the ledger's own)
    - non-flat -> None (the caller renders the scoped computed value)
    """
    if not scope.get("ok"):
        return {"kind": "NA", "value": None,
                "reason": scope.get("reason") or "no authorized scope"}
    if not is_flat:
        return {"kind": "COMPUTED", "value": None, "reason": None}
    if scope.get("mode") == "live":
        if snapshot_ts is None:
            return {"kind": "NA", "value": None,
                    "reason": "live flat UPL requires a fresh verified "
                              "snapshot timestamp (missing)"}
        try:
            age = time.time() - float(snapshot_ts)
        except (TypeError, ValueError):
            return {"kind": "NA", "value": None,
                    "reason": "live flat UPL snapshot timestamp invalid"}
        if age > SNAPSHOT_MAX_AGE_S:
            return {"kind": "NA", "value": None,
                    "reason": f"live flat UPL snapshot stale "
                              f"({age:.0f}s > {SNAPSHOT_MAX_AGE_S:.0f}s)"}
        return {"kind": "ZERO", "value": 0.0, "reason": None}
    return {"kind": "ZERO", "value": 0.0, "reason": None}
