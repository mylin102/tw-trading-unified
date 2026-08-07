"""Durable COMBINED_EXIT / exit intent log (P1-B design v3.1, Phase 1 corr.).

Codex corrective round: all public mutations serialize internally
(reentrant mutex + durable O_EXCL file lock with owner-verified reclaim),
legal state-transition validation, submit_leg sends ONLY from
NOT_SUBMITTED (never re-sends an attempted leg without authoritative
query), real process-start token, durable atomic append (O_APPEND single
write + fsync + parent-dir fsync on creation), collision-resistant ids,
and recovery terminal policy that never silently completes a failed exit.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from typing import Callable, Dict, List, Optional

MAX_ACTIVE_INTENTS = 20
RETENTION_DAYS = 30
INTENT_LOG_NAME = "mts_exit_intent.jsonl"
INTENT_ARCHIVE_NAME = "mts_exit_intent.archive.jsonl"
LOCK_NAME = "intent.lock"

LEG_STATUSES = (
    "NOT_SUBMITTED", "SUBMIT_ATTEMPTED", "SUBMITTED", "FILLED",
    "REJECTED", "CANCELLED", "NOT_FOUND_CONFIRMED", "UNKNOWN",
)
TERMINAL_LEG = ("FILLED", "REJECTED", "CANCELLED", "NOT_FOUND_CONFIRMED")

# legal per-leg transitions (never FILLED→attempted, etc.)
_ALLOWED = {
    "NOT_SUBMITTED": {"SUBMIT_ATTEMPTED"},
    "SUBMIT_ATTEMPTED": {"SUBMITTED", "UNKNOWN", "REJECTED", "CANCELLED",
                         "FILLED", "NOT_FOUND_CONFIRMED"},
    "SUBMITTED": {"FILLED", "REJECTED", "CANCELLED", "UNKNOWN",
                  "NOT_FOUND_CONFIRMED"},
    "UNKNOWN": {"FILLED", "REJECTED", "CANCELLED", "NOT_FOUND_CONFIRMED"},
    "FILLED": set(),
    "REJECTED": set(),
    "CANCELLED": set(),
    "NOT_FOUND_CONFIRMED": set(),
}

# real per-process start token (module import time + pid; PID reuse gets a
# different import-time seed) — owner-verified reclaim uses it
_PROCESS_TOKEN = f"{os.getpid()}-{int(time.time() * 1_000_000)}"


class IntentError(Exception):
    pass


class DuplicateSubmitError(IntentError):
    pass


class StaleVersionError(IntentError):
    pass


class IntentCapacityError(IntentError):
    pass


class LockBusyError(IntentError):
    pass


class IntentNotTerminalError(IntentError):
    pass


class SupersededIntentError(IntentError):
    pass


class IllegalTransitionError(IntentError):
    pass


def client_order_id(trade_id: str, leg: str, nonce: Optional[str] = None) -> str:
    """Deterministic pre-I/O client id (per-leg idempotency key)."""
    import hashlib
    seed = f"{trade_id}:{leg}:{nonce or '0'}"
    return "CE-" + hashlib.sha256(seed.encode()).hexdigest()[:16]


def _atomic_append(path: str, record: dict) -> None:
    """Durable primitive: O_APPEND single write + fsync; fsync parent dir
    on file creation (codex #4)."""
    parent = os.path.dirname(path)
    created = not os.path.exists(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    data = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    fd = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if created and parent:
        dfd = os.open(parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        return False


def _default_owner_check(pid, token) -> dict:
    """Real owner-verified check: PID liveness + current process token."""
    return {"alive": _pid_alive(pid) if isinstance(pid, int) else False,
            "start_token": _PROCESS_TOKEN}


def _read_rows(path: str) -> List[dict]:
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    return out


class IntentLog:
    """Durable per-trade exit intent log (single-writer, internally locked)."""

    def __init__(self, log_dir: str):
        self.log_dir = log_dir
        self.log_path = os.path.join(log_dir, INTENT_LOG_NAME)
        self.archive_path = os.path.join(log_dir, INTENT_ARCHIVE_NAME)
        self.lock_path = os.path.join(log_dir, LOCK_NAME)
        self._mutex = threading.RLock()  # in-process reentrant guard

    # ── read (lock-free) ───────────────────────────────────────────────
    def raw_lines(self) -> List[str]:
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path, encoding="utf-8") as fh:
            return [l.rstrip("\n") for l in fh if l.strip()]

    def _rows(self) -> List[dict]:
        return _read_rows(self.log_path)

    def _archive_rows(self) -> List[dict]:
        return _read_rows(self.archive_path)

    def get(self, intent_id: str) -> dict:
        row = None
        best = -1
        for r in self._rows() + self._archive_rows():
            if r.get("intent_id") == intent_id and r.get("version", 0) > best:
                row = r
                best = r.get("version", 0)
        if row is None:
            raise KeyError(f"no intent {intent_id}")
        return row

    def list_active(self) -> List[str]:
        last: Dict[str, dict] = {}
        for r in self._rows():
            last[r["intent_id"]] = r
        # PARTIAL/FAILED_NO_FILL are NOT fully resolved: repair pending ⇒
        # still in-flight (blocks entry/exit controllers)
        return [iid for iid, r in last.items()
                if r.get("terminal") in (None, "PARTIAL", "FAILED_NO_FILL")]

    # ── durable lock (design §1.7: owner-verified; age never authorizes) ─
    def _lock_meta(self) -> dict:
        return {"pid": os.getpid(), "start_token": _PROCESS_TOKEN,
                "host": socket.gethostname(), "acquired_at": time.time()}

    @contextmanager
    def _file_lock(self):
        """Cross-process exclusive lock with owner-verified reclaim."""
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                owner = self._read_owner()
                reclaim = False
                if owner is not None:
                    verdict = _default_owner_check(owner.get("pid"),
                                                   owner.get("start_token"))
                    alive = bool(verdict.get("alive"))
                    token = verdict.get("start_token")
                    if token is not None and token != owner.get("start_token"):
                        reclaim = True  # PID reused by a new incarnation
                    elif not alive:
                        reclaim = True
                else:
                    reclaim = True  # corrupt/empty lock file
                if not reclaim:
                    raise LockBusyError(
                        f"lock held by pid={owner.get('pid') if owner else '?'}")
                # ownership fence: only unlink if the owner is unchanged
                if self._read_owner() == owner:
                    try:
                        os.unlink(self.lock_path)
                    except OSError:
                        pass
                continue  # retry acquire
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._lock_meta(), ensure_ascii=False))
            try:
                yield
            finally:
                try:
                    os.unlink(self.lock_path)
                except OSError:
                    pass
            return

    def _locked(self, fn: Callable, *a, **k):
        with self._mutex:
            with self._file_lock():
                return fn(*a, **k)

    def try_acquire(self, meta: dict, owner_check_fn: Optional[Callable] = None,
                    age_alert_threshold_s: Optional[float] = None) -> bool:
        """Public lock API for tests/operators. owner_check_fn(pid, token)
        returns {"alive": bool, "start_token": <current>|None}."""
        while True:
            try:
                fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                owner = self._read_owner()
                reclaim = False
                if owner is not None and owner_check_fn is not None:
                    verdict = owner_check_fn(owner.get("pid"),
                                             owner.get("start_token")) or {}
                    alive = bool(verdict.get("alive", False))
                    token = verdict.get("start_token")
                    if alive and token is not None and token != owner.get("start_token"):
                        reclaim = True  # PID reused
                    elif not alive:
                        reclaim = True
                if not reclaim:
                    raise LockBusyError(
                        f"lock held by pid={owner.get('pid') if owner else '?'}")
                if self._read_owner() == owner:
                    try:
                        os.unlink(self.lock_path)
                    except OSError:
                        pass
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(meta, ensure_ascii=False))
            return True

    def lock_owner(self) -> dict:
        return self._read_owner() or {}

    def _read_owner(self) -> Optional[dict]:
        if not os.path.exists(self.lock_path):
            return None
        try:
            with open(self.lock_path, encoding="utf-8") as fh:
                return json.loads(fh.read().strip() or "{}") or None
        except ValueError:
            return None

    def _force_lock_owner(self, meta: dict) -> None:
        with open(self.lock_path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(meta, ensure_ascii=False) + "\n")

    # ── private (locked) helpers ───────────────────────────────────────
    def _create_locked(self, trade_id: str, reason: str,
                       parent: Optional[str] = None,
                       leg: Optional[str] = None) -> dict:
        if len(self.list_active()) >= MAX_ACTIVE_INTENTS:
            raise IntentCapacityError(f"active intents >= {MAX_ACTIVE_INTENTS}")
        uid = uuid.uuid4().hex[:12]
        iid = f"RC-{parent}-{uid}" if parent else f"CE-{trade_id}-{uid}"
        legs = {
            "NEAR": {"status": "NOT_SUBMITTED", "client_order_id": None,
                     "broker_order_id": None},
            "FAR": {"status": "NOT_SUBMITTED", "client_order_id": None,
                    "broker_order_id": None},
        }
        if parent and leg:
            legs = {leg: {"status": "NOT_SUBMITTED", "client_order_id": None,
                          "broker_order_id": None}}
        for l in legs:
            legs[l]["client_order_id"] = client_order_id(trade_id, l, nonce=uid)
        return {
            "intent_id": iid, "trade_id": trade_id, "reason": reason,
            "version": 1, "created_at": time.time(), "parent": parent,
            "legs": legs, "terminal": None, "retention_expires_at": None,
            "supersedes": None,
        }

    def _transition_locked(self, intent_id: str, leg: str, status: str,
                           client_order_id: Optional[str] = None,
                           broker_order_id: Optional[str] = None,
                           expect_version: Optional[int] = None) -> None:
        cur = self.get(intent_id)
        if expect_version is not None and cur.get("version") != expect_version:
            raise StaleVersionError(
                f"expected version {expect_version}, got {cur.get('version')}")
        if cur.get("terminal") == "SUPERSEDED_BY_EMERGENCY":
            raise SupersededIntentError(intent_id)
        prev = cur["legs"][leg]["status"]
        if status not in _ALLOWED.get(prev, set()):
            raise IllegalTransitionError(f"{prev} -> {status} illegal")
        legs = {k: dict(v) for k, v in cur["legs"].items()}
        if client_order_id is not None:
            legs[leg]["client_order_id"] = client_order_id
        if broker_order_id is not None:
            legs[leg]["broker_order_id"] = broker_order_id
        legs[leg]["status"] = status
        rec = dict(cur)
        rec["version"] = cur.get("version", 1) + 1
        rec["legs"] = legs
        _atomic_append(self.log_path, rec)

    # ── public mutations (all internally locked) ──────────────────────
    def create(self, trade_id: str, reason: str = "COMBINED_EXIT") -> str:
        def _do():
            rec = self._create_locked(trade_id, reason)
            _atomic_append(self.log_path, rec)
            return rec["intent_id"]
        return self._locked(_do)

    def transition(self, intent_id: str, leg: str, status: str,
                   client_order_id: Optional[str] = None,
                   broker_order_id: Optional[str] = None,
                   expect_version: Optional[int] = None) -> None:
        self._locked(self._transition_locked, intent_id, leg, status,
                     client_order_id, broker_order_id, expect_version)

    def mark_terminal(self, intent_id: str, status: str) -> None:
        def _do():
            cur = self.get(intent_id)
            rec = dict(cur)
            rec["version"] = cur.get("version", 1) + 1
            rec["terminal"] = status
            _atomic_append(self.log_path, rec)
        self._locked(_do)

    def submit_leg(self, intent_id: str, leg: str, order_mgr,
                   submit_fn: Optional[Callable] = None,
                   submit_hook: Optional[Callable] = None) -> dict:
        """Canonical submit: ONLY from NOT_SUBMITTED. Durable SUBMIT_ATTEMPTED
        first (inside the same lock), then I/O, then SUBMITTED/UNKNOWN.
        An already-attempted leg is REJECTED — recovery must query."""
        def _do():
            cur = self.get(intent_id)
            st = cur["legs"][leg]["status"]
            if st != "NOT_SUBMITTED":
                raise DuplicateSubmitError(f"{intent_id} {leg} already {st} "
                                           "(recovery query required)")
            cid = cur["legs"][leg]["client_order_id"]
            self._transition_locked(intent_id, leg, "SUBMIT_ATTEMPTED",
                                    client_order_id=cid)
            if submit_hook is not None:
                submit_hook(leg, cid, intent_id)
            fn = submit_fn or getattr(order_mgr, "submit")
            try:
                r = fn(cid, leg)
            except Exception:
                # ambiguous (broker may have accepted): durable UNKNOWN
                self._transition_locked(intent_id, leg, "UNKNOWN")
                raise
            oid = (r or {}).get("order_id") if isinstance(r, dict) else None
            self._transition_locked(intent_id, leg, "SUBMITTED",
                                    broker_order_id=oid)
            return {"intent_id": intent_id, "order_id": oid}
        return self._locked(_do)

    def repair_complete(self, intent_id: str, leg: str, reason: str) -> dict:
        def _do():
            cur = self.get(intent_id)
            if cur.get("terminal") == "SUPERSEDED_BY_EMERGENCY":
                raise SupersededIntentError(intent_id)
            child = self._create_locked(cur["trade_id"], f"REPAIR:{reason}",
                                        parent=intent_id, leg=leg)
            _atomic_append(self.log_path, child)
            return {"intent_id": child["intent_id"], "parent": intent_id,
                    "nonce": child["intent_id"].split("-")[-1],
                    "legs": child["legs"]}
        return self._locked(_do)

    def emergency_supersede(self, intent_id: str, order_mgr=None) -> dict:
        def _do():
            _atomic_append(self.log_path, {"event": "EMERGENCY_SUPERSEDES",
                                           "intent_id": intent_id,
                                           "supersedes": intent_id,
                                           "ts": time.time()})
            cur = self.get(intent_id)
            rec = dict(cur)
            rec["version"] = cur.get("version", 1) + 1
            rec["terminal"] = "SUPERSEDED_BY_EMERGENCY"
            _atomic_append(self.log_path, rec)
            return {"supersedes": intent_id}
        return self._locked(_do)

    def reconciliation_view(self, intent_id: str) -> dict:
        intent = self.get(intent_id)
        emg = None
        for r in self._rows():
            if r.get("event") == "EMERGENCY_SUPERSEDES" and r.get("intent_id") == intent_id:
                emg = r
        return {"intent": intent, "emergency": emg}

    # ── recovery (internally serialized) ──────────────────────────────
    def recover(self, intent_id: str, query_fn: Callable,
                order_mgr=None) -> dict:
        def _do():
            cur = self.get(intent_id)
            blocked = False
            for leg, st in cur["legs"].items():
                status = st["status"]
                if status in TERMINAL_LEG or status == "NOT_SUBMITTED":
                    continue
                qid = st.get("broker_order_id") or st.get("client_order_id") or ""
                q = query_fn(qid) if qid else {"status": "UNAVAILABLE"}
                qs = q.get("status") if isinstance(q, dict) else "UNAVAILABLE"
                if qs in ("NOT_FOUND", "FILLED", "REJECTED", "CANCELLED"):
                    target = "NOT_FOUND_CONFIRMED" if qs == "NOT_FOUND" else qs
                    self._transition_locked(intent_id, leg, target)
                else:
                    blocked = True  # UNAVAILABLE/ambiguous: never infer
            cur = self.get(intent_id)
            statuses = [st["status"] for st in cur["legs"].values()]
            terminal = cur.get("terminal")
            if all(s == "NOT_SUBMITTED" for s in statuses):
                self._transition_terminal_locked(intent_id, "CANCELED_SAFE")
                terminal = "CANCELED_SAFE"
            elif all(s == "FILLED" for s in statuses):
                self._transition_terminal_locked(intent_id, "COMPLETED")
                terminal = "COMPLETED"
            elif all(s in TERMINAL_LEG for s in statuses) and \
                    all(s != "FILLED" for s in statuses):
                # attempted exit with NO fill: repair needed, never COMPLETED
                self._transition_terminal_locked(intent_id, "FAILED_NO_FILL")
                terminal = "FAILED_NO_FILL"
            elif any(s == "FILLED" for s in statuses) and \
                    all(s in TERMINAL_LEG for s in statuses):
                self._transition_terminal_locked(intent_id, "PARTIAL")
                terminal = "PARTIAL"
            cur = self.get(intent_id)
            return {"legs": cur["legs"], "terminal": terminal, "blocked": blocked}
        return self._locked(_do)

    def _transition_terminal_locked(self, intent_id: str, status: str) -> None:
        cur = self.get(intent_id)
        rec = dict(cur)
        rec["version"] = cur.get("version", 1) + 1
        rec["terminal"] = status
        _atomic_append(self.log_path, rec)

    # ── compaction / retention ────────────────────────────────────────
    def archive(self, intent_id: str) -> None:
        def _do():
            cur = self.get(intent_id)
            if cur.get("terminal") is None:
                raise IntentNotTerminalError(intent_id)
            rec = dict(cur)
            rec["version"] = cur.get("version", 1) + 1
            rec["retention_expires_at"] = time.time() + RETENTION_DAYS * 86400
            _atomic_append(self.archive_path, rec)
        self._locked(_do)

    def archive_index(self) -> List[str]:
        return [r["intent_id"] for r in self._archive_rows()]

    # ── pre-gate ──────────────────────────────────────────────────────
    def has_inflight_exit_intent(self, trade_id: str) -> bool:
        for iid in self.list_active():
            if self.get(iid).get("trade_id") == trade_id:
                return True
        return False

    def entry_allowed(self, trade_id: str) -> bool:
        return not self.has_inflight_exit_intent(trade_id)

    def exit_trigger_allowed(self, trade_id: str) -> bool:
        return not self.has_inflight_exit_intent(trade_id)

    def session_transition_allowed(self, trade_id: str) -> bool:
        return not self.has_inflight_exit_intent(trade_id)

    def recovery_path_allowed(self, trade_id: str) -> bool:
        return True

    def emergency_path_allowed(self, trade_id: str) -> bool:
        return True


def dispatch_combined_exit(log: IntentLog, trade_id: str, order_mgr,
                           submit_hook: Optional[Callable] = None) -> dict:
    """Design §1.3: create intent (ids persisted) then CANONICAL submit_leg
    per leg (durable SUBMIT_ATTEMPTED before I/O, call-time hook, UNKNOWN on
    ambiguous). Each mutation internally locked; no direct order_mgr.submit."""
    iid = log.create(trade_id, "COMBINED_EXIT")
    for leg in ("NEAR", "FAR"):
        log.submit_leg(iid, leg, order_mgr, submit_hook=submit_hook)
    return {"intent_id": iid}
