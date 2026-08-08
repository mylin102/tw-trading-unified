"""Execution-context persistence (Step 6 — research/core wiring only).

Canonical file: {TRADING_RUNTIME_DIR}/execution_context.json
(authority = core.runtime_paths.runtime_root; TRADING_RUNTIME_DIR env,
fallback repo root — matches the ledger authority).

Contracts (live_route_certification_phase2 §8.4 / §9.3):
- atomic write: tmp + flush + fsync(file) + os.replace + fsync(parent)
- read fail-closed: missing -> LIVE_QUARANTINED (RESTART_MAINTAIN_QUARANTINE);
  corrupt / schema-invalid -> LIVE_QUARANTINED (STATE_FILE_CORRUPTED)
- a persistence failure never enables LIVE: the reader is file-based, so a
  failed atomic write leaves the LAST GOOD state in place
- schema: requested_mode, effective_mode, live_order_allowed,
  audit_reasons (finite strings), revision, updated_at, plus the safe
  session/certificate/lifecycle fields exposed by ExecutionContext.to_dict
  (hashes/ids only — no broker secrets)
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

SCHEMA_KEYS = (
    "requested_mode",
    "effective_mode",
    "live_order_allowed",
    "audit_reasons",
    "revision",
    "updated_at",
    "account_id_hash",
    "session_id",
    "process_start_id",
    "config_hash",
    "state_namespace",
)

# field -> (type, required)
_TYPE_RULES = {
    "requested_mode": (str, True),
    "effective_mode": (str, True),
    "live_order_allowed": (bool, True),
    "audit_reasons": (list, True),
    "revision": (int, True),
    "updated_at": (str, False),
    "account_id_hash": (str, False),
    "session_id": (str, False),
    "process_start_id": (str, False),
    "config_hash": (str, False),
    "state_namespace": (str, False),
}


def _path(runtime_dir: str | None = None) -> str:
    if runtime_dir is not None:
        return os.path.join(runtime_dir, "execution_context.json")
    from core.runtime_paths import runtime_path
    return runtime_path("execution_context.json")


def _fsync_parent(path: str) -> None:
    try:
        dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        # parent fsync is best-effort on some platforms; the replace
        # already happened — do not turn a durability nicety into failure
        pass


def _validate(data: object) -> list:
    """Return schema violations (finite list of strings)."""
    errs: list = []
    if not isinstance(data, dict):
        return ["schema: not an object"]
    for key, (typ, required) in _TYPE_RULES.items():
        if key not in data:
            if required:
                errs.append(f"schema: missing required key {key}")
            continue
        val = data[key]
        if val is None and not required:
            # optional safe fields may be null (e.g. no account hashed yet)
            continue
        if not isinstance(val, typ):
            errs.append(f"schema: {key} is {type(val).__name__}, "
                        f"expected {typ.__name__}")
            continue
        if key == "audit_reasons":
            if not all(isinstance(r, str) for r in val):
                errs.append("schema: audit_reasons must be finite strings")
            if len(val) > 64:
                errs.append("schema: audit_reasons too many entries")
        if key in ("revision",) and isinstance(val, bool):
            errs.append("schema: revision must be int (bool excluded)")
    return errs


def persist_execution_context(ctx_dict: dict, *, runtime_dir: str | None = None,
                              updated_at: str | None = None) -> dict:
    """Atomically persist a context dict. Never leaves a torn file.

    revision: bumped on every successful write (1-based).
    Returns the persisted payload (including revision/updated_at).
    Raises on failure — the caller keeps the in-memory ctx; the file
    retains the last good state (reader is file-based -> never enables LIVE
    from a failed write).
    """
    path = _path(runtime_dir)
    payload = dict(ctx_dict)
    prev_revision = payload.get("revision")
    if not (isinstance(prev_revision, int)
            and not isinstance(prev_revision, bool)):
        # derive from the file's last committed revision (single-writer
        # canonical file); missing/unreadable -> fresh revision 1
        try:
            with open(path, encoding="utf-8") as _f:
                _old = json.load(_f)
            _old_rev = _old.get("revision") if isinstance(_old, dict) else None
            if isinstance(_old_rev, int) and not isinstance(_old_rev, bool):
                prev_revision = _old_rev
        except (OSError, ValueError):
            prev_revision = None
    payload["revision"] = (
        int(prev_revision) + 1
        if isinstance(prev_revision, int) and not isinstance(prev_revision, bool)
        else 1
    )
    payload["updated_at"] = updated_at or datetime.now(
        timezone.utc).isoformat(timespec="milliseconds")
    for key in SCHEMA_KEYS:
        if key not in payload:
            payload[key] = None
    errs = _validate(payload)
    if errs:
        raise ValueError("; ".join(errs))

    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=os.path.dirname(path), prefix=".exec_ctx_", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
        _fsync_parent(path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return payload


def read_execution_context(runtime_dir: str | None = None) -> dict:
    """Fail-closed read.

    missing  -> LIVE_QUARANTINED (RESTART_MAINTAIN_QUARANTINE)
    corrupt / schema-invalid -> LIVE_QUARANTINED (STATE_FILE_CORRUPTED)
    """
    path = _path(runtime_dir)
    if not os.path.exists(path):
        return _fail_closed(("RESTART_MAINTAIN_QUARANTINE",))
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return _fail_closed(("STATE_FILE_CORRUPTED",))
    errs = _validate(data)
    if errs:
        return _fail_closed(tuple(errs) + ("STATE_FILE_CORRUPTED",))
    return data


def _fail_closed(reasons: tuple) -> dict:
    return {
        "requested_mode": "live",
        "effective_mode": "live_quarantined",
        "live_order_allowed": False,
        "audit_reasons": list(reasons),
        "revision": 0,
        "updated_at": None,
        "account_id_hash": None,
        "session_id": None,
        "process_start_id": None,
        "config_hash": None,
        "state_namespace": "live",
    }
